"""Send a message by SMTP, with the statement PDF as attachment."""

import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from pathlib import Path


class PartialDelivery(Exception):
    """The server accepted the message for some recipients and refused it for others."""


class NotSent(Exception):
    """The message was certainly not accepted: the failure came before the server took the message."""


def send(subject: str, text: str, *, host: str, port: int, user: str, password: str, sender: str,
         recipients: list[str], cc: list[str] | None = None, attachment: Path | None = None) -> None:
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = sender
    message["To"] = ", ".join(recipients)
    if cc:
        message["Cc"] = ", ".join(cc)
    message["Date"] = formatdate(localtime=True)
    message["Message-ID"] = make_msgid(domain=sender.rpartition("@")[2].strip("> ") or None)
    message.set_content(text)
    if attachment:
        message.add_attachment(attachment.read_bytes(), maintype="application", subtype="pdf",
                               filename=attachment.name)

    # Port 465 is TLS from the start; every other port must upgrade with STARTTLS before the login.
    context = ssl.create_default_context()
    try:
        if port == 465:
            smtp = smtplib.SMTP_SSL(host, port, context=context, timeout=60)
        else:
            smtp = smtplib.SMTP(host, port, timeout=60)
            smtp.starttls(context=context)
        smtp.login(user, password)
    except (OSError, smtplib.SMTPException) as error:
        raise NotSent(f"no session with {host}:{port}: {error}") from error
    try:
        # An error from here on that is not a clear refusal leaves the result unknown: the caller must not retry.
        refused = smtp.send_message(message)
    except (smtplib.SMTPRecipientsRefused, smtplib.SMTPSenderRefused) as error:
        raise NotSent(f"{host} refused the message: {error}") from error
    finally:
        try:
            smtp.quit()
        except (OSError, smtplib.SMTPException):
            pass
    if refused:
        raise PartialDelivery(f"the mail server refused {', '.join(refused)}; the other recipients got the message")
