"""Find allowlisted mail the bot marked handled but never actually replied to,
and optionally clear the label so the (fixed) classifier reprocesses it.

Why this exists: process_once stamps HANDLED_LABEL on BOTH paths -- after
sending a reply AND after deciding "not a buy request". So when the classifier
broke (GROQ_MODEL switched to a reasoning model while max_tokens was still 3 ->
empty content -> every mail read as NO), the missed mail got labelled and would
never be looked at again. Removing the label puts it back in the bot's query.

Reply detection is per-message via In-Reply-To, NOT per-thread: the broker
reuses one subject, so thread-level checks wrongly report every message in that
long thread as already answered.

  python relabel_skipped.py              # dry run: list what is unreplied
  python relabel_skipped.py --unlabel    # clear the label -> bot will REPLY
"""
import sys
from dotenv import load_dotenv
load_dotenv(".env")
import main

UNLABEL = "--unlabel" in sys.argv
DAYS = 10

svc = main.get_gmail_service()
me = svc.users().getProfile(userId="me").execute()["emailAddress"]
label_id = main.get_or_create_label(svc, main.HANDLED_LABEL)
print(f"mailbox {me} | label {main.HANDLED_LABEL} | window {DAYS}d\n")

refs = svc.users().messages().list(
    userId="me", q=f"in:inbox label:{main.HANDLED_LABEL} newer_than:{DAYS}d",
    maxResults=100).execute().get("messages", [])

rows = []
for ref in refs:
    m = svc.users().messages().get(userId="me", id=ref["id"], format="metadata",
        metadataHeaders=["From", "Subject", "Date", "Message-ID"]).execute()
    h = m["payload"]["headers"]
    if not main.is_allowed(main.parse_sender(main.header(h, "From"))):
        continue
    mid = main.header(h, "Message-ID").strip()
    th = svc.users().threads().get(userId="me", id=m["threadId"],
                                   format="metadata").execute()
    replied = any(
        "SENT" in tm.get("labelIds", [])
        and mid
        and main.header(tm["payload"]["headers"], "In-Reply-To").strip() == mid
        for tm in th["messages"]
    )
    rows.append((int(m["internalDate"]), ref["id"],
                 main.header(h, "Date")[:22], main.header(h, "Subject"), replied))

rows.sort()
unreplied = [r for r in rows if not r[4]]
for _, _, date, subj, replied in rows:
    print(f"  {'replied      ' if replied else 'NO REPLY SENT'}  {date}  {subj[:56]!r}")

print(f"\n{len(rows)} labelled, {len(unreplied)} never replied to")
if not unreplied:
    raise SystemExit

if not UNLABEL:
    print("\ndry run. Re-run with --unlabel to clear the label on the "
          f"{len(unreplied)} message(s) above --\nthe bot will then classify "
          "them and SEND REAL REPLIES to the brokers + everyone on Cc.")
    raise SystemExit

for _, mid, date, subj, _ in unreplied:
    svc.users().messages().modify(userId="me", id=mid,
        body={"removeLabelIds": [label_id]}).execute()
    print(f"  unlabelled  {date}  {subj[:56]!r}")
print(f"\n{len(unreplied)} message(s) will be reclassified on the next poll")
