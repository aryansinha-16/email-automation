"""
One-time OAuth consent to get a refresh token for aryan@valuecart.in.

Run this ONCE locally:
    python get_token.py

It opens a browser, you log in as aryan@valuecart.in and approve. It then
prints the refresh token + client id/secret you paste into Railway env vars.
After that, the bot (main.py) runs forever without any further login.
"""

import json

from google_auth_oauthlib.flow import InstalledAppFlow

CLIENT_SECRET_FILE = r"C:\Users\syste\Downloads\client_secret_1062923913182-35c6hadolm642amoloqq88rj3goh2ems.apps.googleusercontent.com.json"
SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.send",
]

flow = InstalledAppFlow.from_client_secrets_file(CLIENT_SECRET_FILE, SCOPES)
# Desktop client: the library picks a free localhost port automatically — no
# redirect URI registration needed.
creds = flow.run_local_server(
    port=0,
    authorization_prompt_message="Opening browser — log in as aryan@valuecart.in and approve.",
    access_type="offline",
    prompt="consent",  # force a refresh_token even on re-consent
)

with open(CLIENT_SECRET_FILE) as f:
    secret = json.load(f)
key = "web" if "web" in secret else "installed"
client_id = secret[key]["client_id"]
client_secret = secret[key]["client_secret"]

print("\n" + "=" * 70)
print("SUCCESS — copy these into Railway env vars:")
print("=" * 70)
print("GMAIL_CLIENT_ID     =", client_id)
print("GMAIL_CLIENT_SECRET =", client_secret)
print("GMAIL_REFRESH_TOKEN =", creds.refresh_token)
print("=" * 70)
print("\nVerifying access...")

from googleapiclient.discovery import build

svc = build("gmail", "v1", credentials=creds, cache_discovery=False)
prof = svc.users().getProfile(userId="me").execute()
print("Authenticated as:", prof.get("emailAddress"), "| messages:", prof.get("messagesTotal"))
