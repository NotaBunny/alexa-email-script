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
import re
from datetime import datetime, timezone

from bs4 import BeautifulSoup

GMAIL_USER = os.environ["GMAIL_USER"]
GMAIL_APP_PASSWORD = os.environ["GMAIL_APP_PASSWORD"]

# Matches the actual sender address on 1440's emails.
SENDER_FILTER = "dailydigest@email.join1440.com"

# Phrases to strip out so they don't get read aloud (tune this over time
# as you notice leftover boilerplate in your briefings).
SKIP_PHRASES = [
    "unsubscribe",
    "advertise",
    "sponsored by",
    "view in browser",
    "view email in browser",
    "forward to a friend",
    "update your preferences",
    "privacy policy",
    "in partnership with",
    "first time reading",
    "sign up here",
    "insatiably curious",
    "please support our sponsors",
]


def fetch_latest_digest_html() -> str:
    imap = imaplib.IMAP4_SSL("imap.gmail.com")
    imap.login(GMAIL_USER, GMAIL_APP_PASSWORD)
    imap.select("INBOX")

    # No date filter here on purpose — searching "SINCE today" is fragile
    # around midnight/timezone boundaries on a UTC-based CI runner. We just
    # grab the newest matching email instead, which is what we want anyway.
    status, data = imap.search(None, f'(FROM "{SENDER_FILTER}")')

    if status != "OK" or not data or not data[0]:
        imap.logout()
        raise RuntimeError(
            "No 1440 email found at all. Double-check SENDER_FILTER matches "
            "the exact sender address on a real 1440 email in your inbox."
        )

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


def get_raw_text(html_fragment: str) -> str:
    """Converts an HTML fragment to raw plain text with no filtering applied
    yet — needed because strip_more_links() must run before junk-line
    filtering (see note there)."""
    soup = BeautifulSoup(html_fragment, "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()
    return soup.get_text(separator="\n")


def filter_junk_lines(text: str) -> str:
    """Drops boilerplate lines (SKIP_PHRASES) and stray leftover
    punctuation-only lines. Must run AFTER strip_more_links(), not before —
    otherwise the bare "(" and ")" lines that BeautifulSoup splits onto
    their own line get deleted here first, breaking the pairing that
    strip_more_links needs to correctly bound each "(More)" marker."""
    lines = [line.strip() for line in text.split("\n") if line.strip()]
    filtered = [
        line for line in lines
        if not any(phrase in line.lower() for phrase in SKIP_PHRASES)
        and len(line.strip(" .|()")) > 0
    ]
    return "\n".join(filtered)


def html_fragment_to_text(html_fragment: str) -> str:
    """Full pipeline: raw text -> strip (More) markers -> filter junk lines."""
    text = get_raw_text(html_fragment)
    text = strip_more_links(text)
    return filter_junk_lines(text)


def clean_text(html: str) -> str:
    return html_fragment_to_text(html)


def extract_greeting(text: str) -> str:
    """Pulls out the 'Good morning, it's [date].' line to use as an intro."""
    for line in text.split("\n"):
        if line.strip().lower().startswith("good morning"):
            return line.strip()
    return ""


# 1440 renders each section title (e.g. "Quick Hits", "Humankind") as a
# highlighted span at font-size 24px — visually distinct from the plain
# bold item headlines (font-size 15px <strong> tags) inside each section.
# Matching on this exact markup is far more reliable than guessing from
# flattened text, since headline text alone can look just as "heading-like."
SECTION_HEADING_PATTERN = re.compile(
    r'<span style="font-size:24px"><span style="background-color:#[0-9a-fA-F]{3,6}">(.*?)</span></span>',
    re.IGNORECASE | re.DOTALL,
)


def extract_section_html(html: str, section_name: str) -> str:
    """Returns the raw HTML between a named section heading and the next
    section heading (or end of document if it's the last section)."""
    matches = list(SECTION_HEADING_PATTERN.finditer(html))

    if not matches:
        raise RuntimeError(
            "No section headings found at all — 1440's email template may "
            "have changed. Check the raw HTML structure again."
        )

    start = None
    start_idx = None
    for i, m in enumerate(matches):
        title = BeautifulSoup(m.group(1), "html.parser").get_text(strip=True)
        if title.strip().lower() == section_name.lower():
            start = m.end()
            start_idx = i
            break

    if start is None:
        found = [BeautifulSoup(m.group(1), "html.parser").get_text(strip=True) for m in matches]
        raise RuntimeError(
            f"Could not find a '{section_name}' section heading. "
            f"Sections found in today's email: {found}"
        )

    end = matches[start_idx + 1].start() if start_idx + 1 < len(matches) else len(html)
    return html[start:end]


def extract_quick_hits(html: str):
    """Pulls out just the Quick Hits section as clean text, using the real
    HTML structure to find its boundaries precisely.

    If 1440 changes their template and "Quick Hits" can't be found, falls
    back to reading the entire digest instead of failing the whole run —
    better to get the full briefing occasionally than none at all.

    Returns (text, used_fallback).
    """
    try:
        section_html = extract_section_html(html, "Quick Hits")
        return html_fragment_to_text(section_html), False
    except RuntimeError as e:
        print(f"WARNING: {e}")
        print("Falling back to reading the entire digest instead.")
        return html_fragment_to_text(html), True


def strip_more_links(text: str) -> str:
    """Removes "(More)" / "(More, w/video)" style link markers and the
    leftover " | " separators between them, so Alexa doesn't read them aloud.

    Must run on RAW text (before filter_junk_lines) — see that function's
    docstring for why.

    The inner content is capped at 40 chars and can't contain another "("
    or ")", so even in an unexpected edge case this can't "run away" and
    swallow entire headlines between one leftover "(" and a distant ")".
    """
    text = re.sub(r"\(\s*More[^()]{0,40}\)", "", text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"\s*\|\s*", " ", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*", "\n", text)
    return text.strip()


def build_feed(text: str, title: str = "1440 Quick Hits") -> list:
    now = datetime.now(timezone.utc)
    return [
        {
            "uid": now.strftime("1440-%Y%m%d"),
            "updateDate": now.strftime("%Y-%m-%dT%H:%M:%S.0Z"),
            "titleText": title,
            "mainText": text,
            "redirectionUrl": "https://join1440.com",
        }
    ]


def main():
    html = fetch_latest_digest_html()
    full_text = clean_text(html)  # used only to find the greeting line

    greeting = extract_greeting(full_text)
    quick_hits, used_fallback = extract_quick_hits(html)

    final_text = f"{greeting}\n\n{quick_hits}" if greeting else quick_hits

    title = "1440 Daily Digest" if used_fallback else "1440 Quick Hits"
    feed = build_feed(final_text, title=title)

    with open("feed.json", "w", encoding="utf-8") as f:
        json.dump(feed, f, ensure_ascii=False, indent=2)

    print("feed.json updated successfully.")
    print(f"Mode: {'FULL DIGEST (fallback)' if used_fallback else 'Quick Hits only'}")
    print(f"Digest length: {len(final_text)} characters")


if __name__ == "__main__":
    main()
