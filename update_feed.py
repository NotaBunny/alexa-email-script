"""
Fetches today's 1440 Daily Digest email and writes it into feed.json,
an Alexa Flash Briefing text feed. Alexa reads this aloud using her
own built-in text-to-speech when the "Play Flash Briefing" routine
action runs.

Requires two environment variables (set as GitHub Actions secrets, or
locally for testing):
    GMAIL_USER            - your Gmail address
    GMAIL_APP_PASSWORD    - a Gmail "app password" (not your normal password;
                             generate one at https://myaccount.google.com/apppasswords,
                             requires 2-Step Verification to be enabled)

Install dependency:
    pip install beautifulsoup4
"""

import imaplib
import email
import json
import os
from datetime import datetime, timezone

from bs4 import BeautifulSoup

GMAIL_USER = os.environ["GMAIL_USER"]
GMAIL_APP_PASSWORD = os.environ["GMAIL_APP_PASSWORD"]

# Adjust this to match how 1440's emails actually show up in your inbox.
# Check the "From" field of a real 1440 email and use a snippet of it here,
# e.g. "join1440.com" or "1440 Daily Digest".
SENDER_FILTER = "1440 Daily Digest"

# Phrases to strip out so they don't get read aloud (tune this over time
# as you notice leftover boilerplate in your briefings).
SKIP_PHRASES = [
    "unsubscribe",
    "advertise",
    "sponsored by",
    "view in browser",
    "forward to a friend",
    "update your preferences",
    "privacy policy",
]


def fetch_latest_digest_html() -> str:
    imap = imaplib.IMAP4_SSL("imap.gmail.com")
    imap.login(GMAIL_USER, GMAIL_APP_PASSWORD)
    imap.select("INBOX")

    today = datetime.now().strftime("%d-%b-%Y")
    status, data = imap.search(None, f'(FROM "{SENDER_FILTER}" SINCE "{today}")')

    if status != "OK" or not data or not data[0]:
        imap.logout()
        raise RuntimeError("No 1440 email found for today. Check SENDER_FILTER and that it's arrived yet.")

    latest_id = data[0].split()[-1]
    status, msg_data = imap.fetch(latest_id, "(RFC822)")
    raw_email = msg_data[0][1]
    msg = email.message_from_bytes(raw_email)

    html_body = None
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                html_body = part.get_payload(decode=True).decode(errors="ignore")
                break
    else:
        html_body = msg.get_payload(decode=True).decode(errors="ignore")

    imap.logout()

    if not html_body:
        raise RuntimeError("Found the email but couldn't extract an HTML body from it.")

    return html_body


def clean_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")

    for tag in soup(["script", "style"]):
        tag.decompose()

    text = soup.get_text(separator="\n")
    lines = [line.strip() for line in text.split("\n") if line.strip()]

    filtered = [
        line for line in lines
        if not any(phrase in line.lower() for phrase in SKIP_PHRASES)
    ]

    return "\n".join(filtered)


def build_feed(text: str) -> list:
    now = datetime.now(timezone.utc)
    return [
        {
            "uid": now.strftime("1440-%Y%m%d"),
            "updateDate": now.strftime("%Y-%m-%dT%H:%M:%S.0Z"),
            "titleText": "1440 Daily Digest",
            "mainText": text,
            "redirectionUrl": "https://join1440.com",
        }
    ]


def main():
    html = fetch_latest_digest_html()
    text = clean_text(html)
    feed = build_feed(text)

    with open("feed.json", "w", encoding="utf-8") as f:
        json.dump(feed, f, ensure_ascii=False, indent=2)

    print("feed.json updated successfully.")
    print(f"Digest length: {len(text)} characters")


if __name__ == "__main__":
    main()
