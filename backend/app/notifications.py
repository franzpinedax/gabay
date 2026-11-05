"""Optional caregiver notifications for reminder and health alerts.

Email uses SMTP. SMS uses Twilio's HTTPS API without adding a runtime
dependency. Both channels are disabled until their environment variables
and the caregiver's contact information are configured.
"""
import logging
import os
import smtplib
import urllib.error
import urllib.parse
import urllib.request
from email.message import EmailMessage

logger = logging.getLogger("gabay.notifications")


def _send_email(to_address: str, subject: str, body: str) -> None:
    host = os.getenv("SMTP_HOST")
    if not host:
        logger.info("Email notification skipped: SMTP_HOST is not configured")
        return

    port = int(os.getenv("SMTP_PORT", "587"))
    message = EmailMessage()
    message["From"] = os.getenv("SMTP_FROM", os.getenv("SMTP_USER", ""))
    message["To"] = to_address
    message["Subject"] = subject
    message.set_content(body)

    with smtplib.SMTP(host, port, timeout=10) as server:
        server.ehlo()
        if os.getenv("SMTP_TLS", "true").lower() == "true":
            server.starttls()
            server.ehlo()
        username = os.getenv("SMTP_USER")
        password = os.getenv("SMTP_PASSWORD")
        if username and password:
            server.login(username, password)
        server.send_message(message)
    logger.info("Email notification sent to caregiver address %s", to_address)


def _send_sms(phone_number: str, body: str) -> None:
    account_sid = os.getenv("TWILIO_ACCOUNT_SID")
    auth_token = os.getenv("TWILIO_AUTH_TOKEN")
    from_number = os.getenv("TWILIO_FROM_NUMBER")
    if not all((account_sid, auth_token, from_number)):
        logger.info("SMS notification skipped: Twilio is not configured")
        return

    url = f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Messages.json"
    payload = urllib.parse.urlencode({
        "From": from_number,
        "To": phone_number,
        "Body": body,
    }).encode("utf-8")
    request = urllib.request.Request(url, data=payload, method="POST")
    credentials = f"{account_sid}:{auth_token}".encode("utf-8")
    import base64
    request.add_header("Authorization", f"Basic {base64.b64encode(credentials).decode('ascii')}")
    request.add_header("Content-Type", "application/x-www-form-urlencoded")

    with urllib.request.urlopen(request, timeout=10) as response:
        if response.status >= 300:
            raise urllib.error.HTTPError(url, response.status, "Twilio rejected message", response.headers, None)
    logger.info("SMS notification sent to caregiver number %s", phone_number)


def send_alert_notifications(patient: dict, alert: dict) -> None:
    """Deliver an alert through every configured caregiver channel.

    Notification failures are logged but never prevent the alert from being
    stored or shown in the dashboard.
    """
    patient_name = patient.get("nickname") or patient.get("name") or patient["id"]
    subject = f"Gabay alert for {patient_name}"
    body = f"Gabay alert for {patient_name}:\n\n{alert['message']}\n\nAlert type: {alert['type']}"

    email = patient.get("caregiver_email")
    if email:
        try:
            _send_email(email, subject, body)
        except (OSError, smtplib.SMTPException, ValueError) as error:
            logger.error("Email notification failed for patient %s: %s", patient["id"], error)

    phone = patient.get("caregiver_phone")
    if phone:
        try:
            _send_sms(phone, body)
        except (OSError, urllib.error.URLError, urllib.error.HTTPError, ValueError) as error:
            logger.error("SMS notification failed for patient %s: %s", patient["id"], error)
