"""
Gmail auto-reply bot.

Polls a Google Workspace mailbox (impersonated via a service account with
domain-wide delegation), and for every new UNREAD email from an allow-listed
sender, generates a short 3-4 word plain-text reply with Groq and sends it
automatically. Each handled message is marked read + labeled so it is never
answered twice.

Runs 24/7 as a Railway worker.

Required env vars:
  GMAIL_CLIENT_ID              - OAuth client id (from get_token.py)
  GMAIL_CLIENT_SECRET          - OAuth client secret (from get_token.py)
  GMAIL_REFRESH_TOKEN          - OAuth refresh token (from get_token.py)
  ALLOWED_SENDERS              - comma-separated emails/domains, e.g. "ops@acme.com,@partner.com"
  GROQ_API_KEY                 - Groq API key
Optional:
  POLL_SECONDS                 - how often to check (default 60)
  GROQ_MODEL                   - default "llama-3.3-70b-versatile"
  HANDLED_LABEL                - Gmail label applied after replying (default "AutoReplied")
  DRY_RUN                      - "1" to log replies instead of sending
"""

import base64
import html
import json
import os
import re
import time
import traceback
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import parsedate_to_datetime, parseaddr

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from groq import Groq

try:
    from dotenv import load_dotenv  # local dev only; Railway injects env natively

    load_dotenv()
except ImportError:
    pass

# ---- config ---------------------------------------------------------------

SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",  # read + send + modify labels
    "https://www.googleapis.com/auth/gmail.send",
]

TOKEN_URI = "https://oauth2.googleapis.com/token"
ALLOWED_SENDERS = [
    s.strip().lower() for s in os.environ["ALLOWED_SENDERS"].split(",") if s.strip()
]
POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "60"))
GROQ_MODEL = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")
HANDLED_LABEL = os.environ.get("HANDLED_LABEL", "AutoReplied")
DRY_RUN = os.environ.get("DRY_RUN") == "1"

# Exact text sent (verbatim) when an email is classified as a buy-approval request.
APPROVAL_REPLY = os.environ.get("APPROVAL_REPLY", "Approved, please go ahead.")

groq_client = Groq(api_key=os.environ["GROQ_API_KEY"])


# ---- gmail auth -----------------------------------------------------------

def get_gmail_service():
    """Build a Gmail client from the OAuth refresh token (auto-refreshes access tokens)."""
    creds = Credentials(
        token=None,
        refresh_token=os.environ["GMAIL_REFRESH_TOKEN"],
        client_id=os.environ["GMAIL_CLIENT_ID"],
        client_secret=os.environ["GMAIL_CLIENT_SECRET"],
        token_uri=TOKEN_URI,
        scopes=SCOPES,
    )
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def get_or_create_label(service, name):
    """Return the labelId for `name`, creating it if needed."""
    labels = service.users().labels().list(userId="me").execute().get("labels", [])
    for lb in labels:
        if lb["name"].lower() == name.lower():
            return lb["id"]
    created = (
        service.users()
        .labels()
        .create(
            userId="me",
            body={
                "name": name,
                "labelListVisibility": "labelShow",
                "messageListVisibility": "show",
            },
        )
        .execute()
    )
    return created["id"]


# ---- helpers --------------------------------------------------------------

def header(headers, key):
    for h in headers:
        if h["name"].lower() == key.lower():
            return h["value"]
    return ""


def parse_sender(from_header):
    """Extract bare email from a 'Name <email@x>' header, lowercased."""
    if "<" in from_header and ">" in from_header:
        return from_header.split("<", 1)[1].split(">", 1)[0].strip().lower()
    return from_header.strip().lower()


def is_allowed(sender_email):
    for rule in ALLOWED_SENDERS:
        if rule.startswith("@"):
            if sender_email.endswith(rule):
                return True
        elif sender_email == rule:
            return True
    return False


def _part_charset(payload):
    """Read the declared charset from a MIME part's Content-Type header."""
    ct = header(payload.get("headers", []), "Content-Type")
    m = re.search(r'charset\s*=\s*"?([\w\-]+)"?', ct, re.IGNORECASE)
    return m.group(1) if m else None


def _decode_part(payload):
    """Decode a text part's bytes using its declared charset (lossless when present).

    Falls back to cp1252 then utf-8 if the declared charset is missing/unknown,
    because Outlook-composed mail is very often windows-1252 (smart quotes, dashes,
    fractions) which a hardcoded utf-8 decode would have mangled into U+FFFD.
    """
    raw = base64.urlsafe_b64decode(payload["body"]["data"])
    charset = _part_charset(payload)
    for enc in [charset, "utf-8", "cp1252"]:
        if not enc:
            continue
        try:
            return raw.decode(enc)  # strict — only accept a clean decode
        except (UnicodeDecodeError, LookupError):
            continue
    # Last resort: never raise, never lose the whole body.
    return raw.decode("utf-8", "replace")


def extract_body(payload):
    """Best-effort plain-text body extraction from a Gmail message payload."""
    if payload.get("mimeType") == "text/plain" and payload.get("body", {}).get("data"):
        return _decode_part(payload)
    for part in payload.get("parts", []) or []:
        text = extract_body(part)
        if text:
            return text
    return ""


def is_buy_approval_request(subject, body):
    """
    Ask Groq whether this email is asking the recipient to approve a purchase/buy.

    Tuned to be LENIENT toward yes: if the sender clearly wants to buy/purchase/
    order something and is seeking the recipient's go-ahead, answer yes — even if
    the exact wording differs ("request to buy", "want to buy", "approve the PO",
    "can we proceed with the order", "need your sign-off to purchase", etc.).
    Answer no only for things that are clearly NOT seeking purchase approval
    (FYI updates, "already bought", newsletters, or explicit "do not approve").
    """
    prompt = (
        "Decide if the email below is asking the recipient to APPROVE a "
        "purchase / buy / order (the sender wants a go-ahead to buy something).\n"
        "Be lenient: if it plausibly reads as a request to buy or seeking buy "
        "approval, answer YES even if phrased differently. Answer NO only if it "
        "is clearly not a buy-approval request (FYI, already purchased, or an "
        "explicit 'do not approve').\n"
        "Reply with EXACTLY one word: YES or NO.\n\n"
        f"Subject: {subject}\n\n{body[:2000]}"
    )
    resp = groq_client.chat.completions.create(
        model=GROQ_MODEL,
        messages=[{"role": "user", "content": prompt}],
        temperature=0,
        max_tokens=3,
    )
    answer = resp.choices[0].message.content.strip().upper()
    return answer.startswith("Y")


# Trailing whitespace incl. space, tab, and NBSP -- Outlook-composed bodies put
# whitespace-only " " spacer lines between every real line; these must collapse
# to true blanks or they render as a wall of "> " quoted-empty lines.
_TRAIL_WS = re.compile(r"[ \t\xa0]+$")

# Safety caps so a giant nested forward never produces a multi-megabyte trail.
_MAX_QUOTED_LINES = 200
_MAX_QUOTED_CHARS = 20000


def _gmail_attribution(from_header, date_header):
    """Reproduce the attribution line Gmail's web client emits on Reply.

    Gmail does NOT echo the raw RFC 2822 Date header; it re-renders it in en-US
    12-hour form, e.g. "On Mon, May 25, 2026 at 4:05 PM Name <e@x> wrote:".
    Built manually because %-d/%-I raise ValueError on Windows/Python 3.14.
    Falls back gracefully if the date is missing or unparseable.
    """
    from_header = (from_header or "").strip()
    dt = None
    if date_header:
        try:
            dt = parsedate_to_datetime(date_header)
        except (TypeError, ValueError, IndexError):
            dt = None
    if dt is not None:
        hour12 = ((dt.hour - 1) % 12) + 1  # 0->12, 13->1, 16->4
        stamp = f"{dt:%a}, {dt:%b} {dt.day}, {dt.year} at {hour12}:{dt:%M} {dt:%p}"
        return f"On {stamp} {from_header} wrote:"
    if date_header and from_header:
        return f"On {date_header}, {from_header} wrote:"
    if from_header:
        return f"{from_header} wrote:"
    return "wrote:"


def _clean_lines(body):
    """Normalize newlines, strip trailing whitespace, collapse blank runs.

    Encoding is fixed upstream in extract_body (charset-aware decode), so any
    residual U+FFFD here is genuinely undecodable; drop it rather than guess
    (guessing silently corrupts numbers like "2<FFFD>5 lakhs" -> "25 lakhs").
    """
    s = body.replace("�", "").replace("\r\n", "\n").replace("\r", "\n")
    lines = [_TRAIL_WS.sub("", ln) for ln in s.split("\n")]
    out = []
    prev_blank = False
    for ln in lines:
        blank = ln == ""
        if blank and prev_blank:
            continue  # collapse consecutive blanks to one
        out.append(ln)
        prev_blank = blank
    while out and out[0] == "":
        out.pop(0)
    while out and out[-1] == "":
        out.pop()
    return out


def build_quote(from_header, date_header, original_body):
    """Build the Gmail-style quoted-original block that goes below the reply.

    Produces a localized "On ... wrote:" attribution followed by ">"-prefixed
    lines (bare ">" on blanks, never "> "), the exact pattern Gmail collapses
    behind the "..." (3 dots). Keeps the whole original (Gmail collapses a
    forwarded chain, it does not delete it). Returns "" when there's nothing
    meaningful to quote, so the caller appends it without a dangling attribution.
    """
    lines = _clean_lines(original_body or "")
    if not lines:
        return ""

    if len(lines) > _MAX_QUOTED_LINES:
        lines = lines[:_MAX_QUOTED_LINES] + ["[... quoted message truncated ...]"]

    quoted = "\n".join(">" if ln == "" else "> " + ln for ln in lines)
    if len(quoted) > _MAX_QUOTED_CHARS:
        quoted = quoted[:_MAX_QUOTED_CHARS].rstrip() + "\n> [... quoted message truncated ...]"

    return f"{_gmail_attribution(from_header, date_header)}\n{quoted}"


# Exact inline style Gmail's web client emits on the wire — this is what draws
# the gray vertical quote bar in the recipient's client (the class alone won't).
_GMAIL_BLOCKQUOTE_STYLE = (
    "margin:0px 0px 0px 0.8ex;"
    "border-left:1px solid rgb(204,204,204);"
    "padding-left:1ex"
)


def _esc_br(text):
    """HTML-escape THEN convert newlines to <br> (order matters: escaping first
    keeps the <br> we add from becoming literal &lt;br&gt;)."""
    return html.escape(text or "").replace("\n", "<br>")


def _html_sender(from_header):
    """Render the attribution sender as Gmail does: 'Name &lt;<a ...>addr</a>&gt;'
    with name + address HTML-escaped. Omits the name if there isn't one."""
    name, addr = parseaddr(from_header or "")
    if addr:
        esc_addr = html.escape(addr)
        link = f'&lt;<a href="mailto:{esc_addr}">{esc_addr}</a>&gt;'
        return f"{html.escape(name)} {link}" if name else link
    return html.escape((from_header or "").strip())


def _html_attribution(from_header, date_header):
    """Build the HTML 'On <date> <Name> &lt;email&gt; wrote:' line. Mirrors
    _gmail_attribution's date rendering exactly so the html and plain parts stay
    in sync, but with escaped + mailto-linked sender markup."""
    sender = _html_sender(from_header)

    dt = None
    if date_header:
        try:
            dt = parsedate_to_datetime(date_header)
        except (TypeError, ValueError, IndexError):
            dt = None

    if dt is not None:
        hour12 = ((dt.hour - 1) % 12) + 1  # 0->12, 13->1, 16->4
        stamp = f"{dt:%a}, {dt:%b} {dt.day}, {dt.year} at {hour12}:{dt:%M} {dt:%p}"
        prefix = f"On {html.escape(stamp)} "
    elif date_header and sender:
        prefix = f"On {html.escape(date_header.strip())}, "
    else:
        prefix = ""

    body = f"{prefix}{sender} wrote:" if sender else f"{prefix}wrote:"
    return body.strip()


def _html_quote(reply_text, from_header, date_header, original_body):
    """Build the HTML body: the reply in its own div, then (if there's anything
    to quote) Gmail's gmail_quote container with the gray-bar blockquote. Uses
    the SAME _clean_lines() as the plain part so the two stay in sync, and the
    same line/char caps. Returns just the reply div when there's nothing to quote."""
    reply_html = _esc_br(reply_text)

    lines = _clean_lines(original_body or "")
    if not lines:
        return f'<div dir="ltr">{reply_html}</div>'

    if len(lines) > _MAX_QUOTED_LINES:
        lines = lines[:_MAX_QUOTED_LINES] + ["[... quoted message truncated ...]"]

    original_html = "<br>".join(html.escape(ln) for ln in lines)
    if len(original_html) > _MAX_QUOTED_CHARS:
        original_html = (
            original_html[:_MAX_QUOTED_CHARS].rstrip()
            + "<br>[... quoted message truncated ...]"
        )

    attr_html = _html_attribution(from_header, date_header)
    return (
        f'<div dir="ltr">{reply_html}</div><br>'
        f'<div class="gmail_quote gmail_quote_container">'
        f'<div dir="ltr" class="gmail_attr">{attr_html}<br></div>'
        f'<blockquote class="gmail_quote" style="{_GMAIL_BLOCKQUOTE_STYLE}">'
        f"{original_html}"
        f"</blockquote>"
        f"</div>"
    )


def build_reply_raw(
    to_email, subject, in_reply_to, references, reply_text,
    from_header="", date_header="", original_body="",
):
    """Build a base64url raw MIME reply that threads correctly, as a
    multipart/alternative: a text/plain part ('>'-quoted) AND a text/html part
    (Gmail gmail_quote gray-bar blockquote). Per RFC 2046 the LAST alternative
    is preferred, so plain comes first and html last -> Gmail renders the gray
    bar (no literal '>'); text clients still get a readable fallback."""
    reply_subject = subject if subject.lower().startswith("re:") else f"Re: {subject}"

    # Plain part: reuse the '>'-quoted builder verbatim (returns "" when empty,
    # so no dangling "On ..." attribution).
    quote = build_quote(from_header, date_header, original_body)
    plain_body = f"{reply_text}\n\n{quote}" if quote else reply_text

    # HTML part: the gmail_quote blockquote (the gray bar).
    html_body = _html_quote(reply_text, from_header, date_header, original_body)

    msg = MIMEMultipart("alternative")
    msg["To"] = to_email
    # From is omitted: Gmail sends as the authenticated account automatically.
    msg["Subject"] = reply_subject
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = (references + " " + in_reply_to).strip()

    msg.attach(MIMEText(plain_body, "plain", "utf-8"))  # FIRST (least preferred)
    msg.attach(MIMEText(html_body, "html", "utf-8"))    # LAST  (most preferred)
    return base64.urlsafe_b64encode(msg.as_bytes()).decode()


# ---- main loop ------------------------------------------------------------

def process_once(service, handled_label_id):
    """Find inbox mail we haven't handled yet, reply if it's a buy request."""
    # Track ONLY our own label — not read/unread. This way the bot still fires
    # even if you (or your phone) opened the email before the poll caught it.
    # We bound it to recent mail so we don't scan the whole inbox each poll.
    query = f"in:inbox -label:{HANDLED_LABEL} newer_than:2d"
    resp = (
        service.users()
        .messages()
        .list(userId="me", q=query, maxResults=25)
        .execute()
    )
    messages = resp.get("messages", [])
    if not messages:
        return

    for ref in messages:
        msg = (
            service.users()
            .messages()
            .get(userId="me", id=ref["id"], format="full")
            .execute()
        )
        headers = msg["payload"]["headers"]
        sender = parse_sender(header(headers, "From"))

        if not is_allowed(sender):
            continue  # leave it untouched (still unread, no label)

        subject = header(headers, "Subject")
        message_id = header(headers, "Message-ID")
        references = header(headers, "References")
        from_header = header(headers, "From")  # full "Name <email>" for the quote line
        date_header = header(headers, "Date")
        body = extract_body(msg["payload"])

        approve = is_buy_approval_request(subject, body)

        if not approve:
            # Not a buy-approval request: do NOT reply, leave it UNREAD so it still
            # shows in your inbox, but label it so we don't re-classify every poll.
            print(f"[{sender}] {subject!r} -> NO (not a buy request, skipped)", flush=True)
            service.users().messages().modify(
                userId="me",
                id=ref["id"],
                body={"addLabelIds": [handled_label_id]},
            ).execute()
            continue

        print(f"[{sender}] {subject!r} -> YES -> {APPROVAL_REPLY!r}", flush=True)

        if not DRY_RUN:
            raw = build_reply_raw(
                sender, subject, message_id, references, APPROVAL_REPLY,
                from_header=from_header, date_header=date_header, original_body=body,
            )
            service.users().messages().send(
                userId="me",
                body={"raw": raw, "threadId": msg["threadId"]},
            ).execute()

        # Approved + replied: remove UNREAD, add our label so we never re-reply.
        service.users().messages().modify(
            userId="me",
            id=ref["id"],
            body={
                "removeLabelIds": ["UNREAD"],
                "addLabelIds": [handled_label_id],
            },
        ).execute()


def main():
    service = get_gmail_service()
    me = service.users().getProfile(userId="me").execute().get("emailAddress")
    print(
        f"Starting auto-reply | mailbox {me} | "
        f"allow={ALLOWED_SENDERS} | poll={POLL_SECONDS}s | dry_run={DRY_RUN}",
        flush=True,
    )
    handled_label_id = get_or_create_label(service, HANDLED_LABEL)

    while True:
        try:
            process_once(service, handled_label_id)
        except Exception:
            print("ERROR in poll loop:", flush=True)
            traceback.print_exc()
            # Rebuild service in case the token/connection went bad.
            try:
                service = get_gmail_service()
                handled_label_id = get_or_create_label(service, HANDLED_LABEL)
            except Exception:
                traceback.print_exc()
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
