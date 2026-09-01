"""
Gmail sender — shared helper for automated checks.

Sends mail as you over Gmail's SMTP server, authenticating with a Gmail
"app password" (a 16-char password just for scripts, made at
myaccount.google.com/apppasswords). Only the Python standard library is used,
so this runs anywhere python3 does — no pip install needed.

The app password is looked up in this order:
  1. the GMAIL_APP_PASSWORD environment variable  (used by GitHub Actions)
  2. the file ~/.config/gmail-automation/app_password  (used by local jobs,
     e.g. the weekly all-weather-risk-scan)

Reuse this file unchanged for other automated checks.

Command-line use (for jobs that shell out rather than import):
  python3 emailer.py "Subject line" "Body text"
  python3 emailer.py "Subject line"          # body read from stdin
"""

import os
import smtplib
import ssl
import sys
from email.mime.text import MIMEText
from pathlib import Path

DEFAULT_RECIPIENT = "louismodell1001@gmail.com"
DEFAULT_SENDER = "louismodell1001@gmail.com"

APP_PASSWORD_FILE = Path.home() / ".config" / "gmail-automation" / "app_password"


def _app_password():
    value = os.environ.get("GMAIL_APP_PASSWORD")
    if not value and APP_PASSWORD_FILE.exists():
        value = APP_PASSWORD_FILE.read_text()
    if not value or not value.strip():
        raise RuntimeError(
            "No Gmail app password found. Set the GMAIL_APP_PASSWORD environment "
            f"variable, or put the 16-character app password in {APP_PASSWORD_FILE}"
        )
    # App passwords are alphanumeric; strip spaces, newlines, and the
    # non-breaking spaces Google's copy-to-clipboard sometimes inserts.
    return "".join(ch for ch in value if ch.isalnum())


def send_email(subject, body, to=DEFAULT_RECIPIENT, sender=DEFAULT_SENDER):
    """Send a plain-text email via Gmail SMTP. Raises on failure.

    `to` may be a single address or a list/tuple of addresses.
    """
    recipients = [to] if isinstance(to, str) else list(to)

    msg = MIMEText(body, "plain")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = ", ".join(recipients)

    context = ssl.create_default_context()
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=context) as server:
        server.login(sender, _app_password())
        server.sendmail(sender, recipients, msg.as_string())


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit('usage: python3 emailer.py "subject" ["body"]   (body else from stdin)')
    subject_arg = sys.argv[1]
    body_arg = sys.argv[2] if len(sys.argv) > 2 else sys.stdin.read()
    send_email(subject_arg, body_arg)
    print("[email sent]")
