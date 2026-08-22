import email as emaillib
import imaplib
import smtplib
from email.header import decode_header, make_header
from email.message import EmailMessage

from . import config, db


class MailNotConfigured(RuntimeError):
    pass


def _creds() -> dict | None:
    address = config.MAIL_ADDRESS
    password = config.MAIL_PASSWORD
    if not address or not password or "@" not in address:
        return None
    domain = address.split("@")[-1].lower()
    smtp_host = config.SMTP_HOST or f"smtp.{domain}"
    imap_host = config.IMAP_HOST or f"imap.{domain}"
    return {
        "address": address,
        "password": password,
        "smtp_host": smtp_host,
        "smtp_port": config.SMTP_PORT,
        "imap_host": imap_host,
        "imap_port": config.IMAP_PORT,
    }


def mail_configured() -> bool:
    return _creds() is not None


def format_draft(to: str, subject: str, body: str) -> str:
    return (
        "DRAFT EMAIL\n"
        f"From:    {_creds()['address'] if _creds() else config.MAIL_ADDRESS or '(mailbox not configured)'}\n"
        f"To:      {to}\n"
        f"Subject: {subject}\n\n"
        f"{body}\n"
        "\n[end of draft]"
    )


def draft_email(to: str, subject: str, body: str) -> str:
    if not to or "@" not in to:
        return "ERROR: a valid recipient address is required."
    return format_draft(to.strip(), subject.strip(), body)


def send_email(to: str, subject: str, body: str, confirm: bool = False) -> str:
    creds = _creds()
    if not creds:
        raise MailNotConfigured(
            "Mailbox not configured. Add JARVIS_MAIL_ADDRESS and JARVIS_MAIL_PASSWORD (app password) to .env."
        )
    if "@" not in (to or ""):
        return "ERROR: a valid recipient address is required."
    if not confirm:
        return (
            format_draft(to.strip(), subject.strip(), body)
            + "\n\nThis has NOT been sent. Ask the user to approve, then call send_email again with confirm=true."
        )
    if db.get_setting("allow_mail_send", "0") != "1":
        return (
            "DENIED: outbound email is disabled by the user. Tell them to enable "
            "'Allow sending real emails' in the Setup tab, then ask again. Do NOT retry until then."
        )
    msg = EmailMessage()
    msg["From"] = creds["address"]
    msg["To"] = to.strip()
    msg["Subject"] = subject.strip()[:200]
    msg.set_content(body[:20000])
    with smtplib.SMTP(creds["smtp_host"], creds["smtp_port"], timeout=30) as server:
        server.starttls()
        server.login(creds["address"], creds["password"])
        server.send_message(msg)
    return f"Sent to {to.strip()}."


def _decode(raw: str) -> str:
    try:
        return str(make_header(decode_header(raw)))
    except Exception:
        return raw


def read_inbox(limit: int = 6, unread_only: bool = False) -> str:
    creds = _creds()
    if not creds:
        raise MailNotConfigured(
            "Mailbox not configured. Add JARVIS_MAIL_ADDRESS and JARVIS_MAIL_PASSWORD (app password) to .env."
        )
    conn = imaplib.IMAP4_SSL(creds["imap_host"], creds["imap_port"])
    try:
        conn.login(creds["address"], creds["password"])
        conn.select("INBOX", readonly=True)
        criteria = "(UNSEEN)" if unread_only else "(ALL)"
        status, data = conn.search(None, criteria)
        if status != "OK":
            return "Could not search the mailbox."
        ids = data[0].split()
        if not ids:
            return "Inbox is empty." if not unread_only else "No unread messages."
        picked = ids[-max(1, min(int(limit), 15)) :][::-1]
        lines = []
        for mid in picked:
            _, msg_data = conn.fetch(mid, "(BODY.PEEK[])")
            raw = msg_data[0][1]
            msg = emaillib.message_from_bytes(raw)
            frm = _decode(msg.get("From", "?"))
            subj = _decode(msg.get("Subject", "(no subject)"))
            date = msg.get("Date", "")[:22]
            body_snip = ""
            if msg.is_multipart():
                for part in msg.walk():
                    if part.get_content_type() == "text/plain":
                        payload = part.get_payload(decode=True)
                        if payload:
                            body_snip = payload.decode(errors="ignore").strip()
                        break
            else:
                payload = msg.get_payload(decode=True)
                body_snip = (payload or b"").decode(errors="ignore").strip()
            lines.append(f"From: {frm}\nSubject: {subj}\nDate: {date}\n{body_snip[:220]}")
        return "\n---\n".join(lines)
    finally:
        try:
            conn.logout()
        except Exception:
            pass
