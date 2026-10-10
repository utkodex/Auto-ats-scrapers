"""Read Greenhouse's application security code from Gmail (IMAP, read-only).

After you click Submit, Greenhouse may ask for an 8-character code it emails to
you ("Security code for your application to <Company>"). fetch_security_code()
waits for that email and returns the code. Messages are read with BODY.PEEK in a
read-only mailbox, so nothing is marked as read, moved or deleted.

Credentials come from the repo's .env (gitignored):
    SENDER_EMAIL=you@gmail.com
    SENDER_PASS=<16-letter Google app password>   (normal passwords don't work with IMAP)
"""

from __future__ import annotations

import contextlib
import email
import imaplib
import re
import time
from datetime import UTC, datetime, timedelta
from email.header import decode_header, make_header
from email.utils import parsedate_to_datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
IMAP_HOST = "imap.gmail.com"
# All Mail also covers Promotions/Updates; INBOX is the fallback name.
MAILBOXES = ('"[Gmail]/All Mail"', "INBOX")
CODE_PATTERN = re.compile(r"application:\s*([A-Za-z0-9]{8})\b")


class OtpError(Exception):
    pass


def load_credentials(env_file: Path = REPO / ".env") -> tuple[str, str]:
    values = {}
    if env_file.exists():
        for line in env_file.read_text("utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                key, _, value = line.partition("=")
                values[key.strip()] = value.strip().strip('"').strip("'")
    address = values.get("GMAIL_ADDRESS") or values.get("SENDER_EMAIL")
    password = values.get("GMAIL_APP_PASSWORD") or values.get("SENDER_PASS")
    if not address or not password:
        raise OtpError(f"Gmail credentials missing - set SENDER_EMAIL and SENDER_PASS in {env_file}")
    return address, password.replace(" ", "")


def _text_of(message: email.message.Message) -> str:
    parts = []
    for part in message.walk():
        if part.get_content_type() in ("text/plain", "text/html"):
            payload = part.get_payload(decode=True) or b""
            parts.append(payload.decode(part.get_content_charset() or "utf-8", "replace"))
    text = re.sub(r"<[^>]+>", " ", " ".join(parts))
    return re.sub(r"\s+", " ", text)


def _open_mailbox(conn: imaplib.IMAP4_SSL) -> None:
    for box in MAILBOXES:
        if conn.select(box, readonly=True)[0] == "OK":
            return
    raise OtpError("Couldn't open the Gmail mailbox")


def _latest_code(conn: imaplib.IMAP4_SSL, since: datetime, company: str | None) -> tuple[str, datetime] | None:
    _open_mailbox(conn)  # re-select so newly arrived mail is visible
    day = (since - timedelta(days=1)).strftime("%d-%b-%Y")
    typ, data = conn.search(None, f'(FROM "greenhouse-mail.io" SUBJECT "Security code" SINCE {day})')
    if typ != "OK":
        return None
    found = []
    for msg_id in reversed(data[0].split()[-20:]):  # newest first
        typ, raw = conn.fetch(msg_id, "(BODY.PEEK[])")
        if typ != "OK" or not raw or not isinstance(raw[0], tuple):
            continue
        message = email.message_from_bytes(raw[0][1])
        try:
            sent = parsedate_to_datetime(message["Date"])
        except (TypeError, ValueError):
            continue
        if sent.tzinfo is None:
            sent = sent.replace(tzinfo=UTC)
        if sent < since:
            continue
        match = CODE_PATTERN.search(_text_of(message))
        if match:
            subject = str(make_header(decode_header(message["Subject"] or "")))
            found.append((sent, match[1], subject))
    if not found:
        return None
    found.sort(reverse=True)
    if company:  # prefer the email for this company, newest first
        for sent, code, subject in found:
            if company.lower().replace("-", " ") in subject.lower():
                return code, sent
    sent, code, _ = found[0]
    return code, sent


def fetch_security_code(since: datetime, company: str | None = None, timeout: float = 180,
                        poll: float = 5) -> tuple[str, datetime]:
    """Wait for a Greenhouse security-code email sent after `since`; returns (code, sent_at)."""
    address, password = load_credentials()
    if since.tzinfo is None:
        since = since.astimezone()
    conn = imaplib.IMAP4_SSL(IMAP_HOST)
    try:
        try:
            conn.login(address, password)
        except imaplib.IMAP4.error as exc:
            raise OtpError("Gmail login failed - check SENDER_EMAIL / SENDER_PASS (must be an app password)") from exc
        deadline = time.time() + timeout
        while True:
            result = _latest_code(conn, since, company)
            if result:
                return result
            if time.time() > deadline:
                raise OtpError(f"No Greenhouse security-code email arrived within {timeout:.0f}s")
            time.sleep(poll)
    finally:
        with contextlib.suppress(Exception):
            conn.logout()
