"""
Email delivery via Resend.
Used by the SuperTokens email delivery override and any direct sending (e.g. invites).

The brand logo is embedded inline via CID (no external image hosting required).
"""

from __future__ import annotations

import base64
import html
import logging
from pathlib import Path

import resend

from config.settings import settings

logger = logging.getLogger(__name__)

_LOGO_CID = "selfbrief-logo"
_LOGO_PATH = Path(__file__).resolve().parent.parent / "assets" / "brand" / "full_logo.png"


def _client() -> None:
    resend.api_key = settings.RESEND_API_KEY


def _logo_attachment() -> dict | None:
    if not _LOGO_PATH.is_file():
        logger.warning("Brand logo missing at %s — emails will send without logo", _LOGO_PATH)
        return None
    content = base64.b64encode(_LOGO_PATH.read_bytes()).decode("ascii")
    return {
        "filename": "full_logo.png",
        "content": content,
        "content_type": "image/png",
        "content_id": _LOGO_CID,
    }


def _email_html(body: str, *, include_logo: bool) -> str:
    """
    Match the sister-app email shell (base.html): navy header, content block,
    orange SelfBrief Limited footer.
    """
    if include_logo:
        brand = (
            f'<img src="cid:{_LOGO_CID}" alt="SelfBrief" '
            'style="max-height: 50px; height: 50px; width: auto; border: 0; display: inline-block;" />'
        )
    else:
        brand = '<span style="font-size: 20px; font-weight: 700; color: #ffffff;">SelfBrief</span>'

    return f"""
    <html lang="">
    <body style="font-family: Arial, Helvetica, sans-serif !important; font-size: 14px; -webkit-font-smoothing: antialiased; padding: 20px 0; margin: 0; background: #ffffff; color: #000000;">
      <div style="max-width: 700px; margin: 0 auto; background: #fff; padding: 20px; border-radius: 5px;">
        <div style="text-align: center; border-radius: 5px; background-color: #152F4D; padding: 10px 16px;">
          {brand}
        </div>
        <div style="padding: 30px 20px;">
          {body}
        </div>
        <div style="margin-bottom: 20px; text-align: center; background-color: #EF7D30; color: #FFFFFF; border-radius: 5px; height: 50px; line-height: 50px;">
          <b><a style="color: #FFFFFF; text-decoration: none;" href="https://www.selfbrief.aero/">SelfBrief Limited</a></b>
        </div>
      </div>
    </body>
    </html>
    """


def _cta(href: str, label: str) -> str:
    """Navy button matching the sister-app .button class."""
    return (
        f'<a href="{href}" target="_blank" '
        'style="background: #152F4D; padding: 10px 15px; color: #fff; text-decoration: none; '
        'display: inline-block; border-radius: 5px; font-family: Arial, Helvetica, sans-serif; '
        'font-size: 14px; font-weight: bold;">'
        f"{label}</a>"
    )


def _p(text: str) -> str:
    return f'<p style="margin: 0 0 16px; line-height: 1.5;">{text}</p>'


def _send(*, to_email: str, subject: str, body: str) -> bool:
    _client()
    attachment = _logo_attachment()
    payload: dict = {
        "from": settings.FROM_EMAIL,
        "to": [to_email],
        "subject": subject,
        "html": _email_html(body, include_logo=attachment is not None),
    }
    if attachment:
        payload["attachments"] = [attachment]

    resend.Emails.send(payload)
    return True


def _airport_label(airport_icao: str | None, airport_name: str | None) -> str:
    icao = (airport_icao or "").strip().upper()
    name = (airport_name or "").strip()
    if icao and name:
        return f"{icao} ({name})"
    return icao or name or "unknown airport"


def _job_refs(job_title: str, airport_icao: str | None, airport_name: str | None) -> tuple[str, str]:
    return html.escape(job_title or ""), html.escape(_airport_label(airport_icao, airport_name))


def send_invite_email(to_email: str, invite_link: str, org_name: str) -> bool:
    """
    Send a branded invite email to a new user.
    Returns True on success, False on failure (so callers can fall back to copy-link).
    """
    if not settings.RESEND_API_KEY:
        logger.warning("RESEND_API_KEY not set — skipping invite email to %s", to_email)
        return False

    org = html.escape(org_name)
    link = html.escape(invite_link)
    try:
        _send(
            to_email=to_email,
            subject=f"You've been invited to {org_name} on SelfBrief",
            body=(
                _p(f"You have been invited to join <b>{org}</b> on SelfBrief.")
                + _p(
                    f"A SelfBrief administrator has invited you to join <b>{org}</b>. "
                    "Click the button below to set your password and access the platform."
                )
                + f"<p style=\"margin: 0 0 16px;\">{_cta(invite_link, 'Accept Invite &amp; Set Password')}</p>"
                + '<p style="border-bottom: 1px solid #e8e8e8; margin: 24px 0;"></p>'
                + _p(
                    "This link expires after use. If you were not expecting this invitation, you can ignore this email."
                )
                + _p(f"Or copy this URL into your browser:<br>{link}")
            ),
        )
        logger.info("Invite email sent to %s", to_email)
        return True
    except Exception as exc:
        logger.error("Failed to send invite email to %s: %s", to_email, exc)
        return False


def send_job_created_email(
    to_email: str,
    job_title: str,
    airport_icao: str | None = None,
    airport_name: str | None = None,
) -> bool:
    """Notify the job creator that their categorisation job has been submitted."""
    if not settings.RESEND_API_KEY:
        logger.warning("RESEND_API_KEY not set — skipping job created email to %s", to_email)
        return False

    title, airport = _job_refs(job_title, airport_icao, airport_name)
    try:
        _send(
            to_email=to_email,
            subject=f"Categorisation started: {job_title}",
            body=(
                _p(
                    f"The Categorisation job titled <b>{title}</b> for airport "
                    f"<b>{airport}</b> has been submitted and categorisation is now in progress."
                )
                + _p("We will send you another email once it is complete.")
                + '<p style="border-bottom: 1px solid #e8e8e8; margin: 24px 0;"></p>'
                + _p("You are receiving this because you created this job on SelfBrief.")
            ),
        )
        logger.info("Job created email sent to %s", to_email)
        return True
    except Exception as exc:
        logger.error("Failed to send job created email to %s: %s", to_email, exc)
        return False


def send_job_completed_email(
    to_email: str,
    job_title: str,
    airport_icao: str | None = None,
    airport_name: str | None = None,
) -> bool:
    """Notify the job creator that their categorisation job completed successfully."""
    if not settings.RESEND_API_KEY:
        logger.warning("RESEND_API_KEY not set — skipping job completed email to %s", to_email)
        return False

    title, airport = _job_refs(job_title, airport_icao, airport_name)
    try:
        _send(
            to_email=to_email,
            subject=f"Categorisation complete: {job_title}",
            body=(
                _p("<b>Completed</b>")
                + _p(
                    f"The Categorisation job titled <b>{title}</b> for airport "
                    f"<b>{airport}</b> has completed successfully. Log in to SelfBrief to view the results."
                )
                + '<p style="border-bottom: 1px solid #e8e8e8; margin: 24px 0;"></p>'
                + _p("You are receiving this because you created this job on SelfBrief.")
            ),
        )
        logger.info("Job completed email sent to %s", to_email)
        return True
    except Exception as exc:
        logger.error("Failed to send job completed email to %s: %s", to_email, exc)
        return False


def send_job_failed_email(
    to_email: str,
    job_title: str,
    airport_icao: str | None = None,
    airport_name: str | None = None,
) -> bool:
    """Notify the job creator that their categorisation job failed."""
    if not settings.RESEND_API_KEY:
        logger.warning("RESEND_API_KEY not set — skipping job failed email to %s", to_email)
        return False

    title, airport = _job_refs(job_title, airport_icao, airport_name)
    try:
        _send(
            to_email=to_email,
            subject=f"Categorisation failed: {job_title}",
            body=(
                _p("<b>Failed</b>")
                + _p(
                    f"The Categorisation job titled <b>{title}</b> for airport "
                    f"<b>{airport}</b> could not be completed."
                )
                + '<p style="border-bottom: 1px solid #e8e8e8; margin: 24px 0;"></p>'
                + _p("You are receiving this because you created this job on SelfBrief.")
            ),
        )
        logger.info("Job failed email sent to %s", to_email)
        return True
    except Exception as exc:
        logger.error("Failed to send job failed email to %s: %s", to_email, exc)
        return False


def send_password_reset_email(to_email: str, reset_link: str) -> bool:
    """
    Called by the SuperTokens email delivery override for forgot-password flows.
    """
    if not settings.RESEND_API_KEY:
        logger.warning("RESEND_API_KEY not set — skipping password reset email to %s", to_email)
        return False

    link = html.escape(reset_link)
    try:
        _send(
            to_email=to_email,
            subject="Reset your SelfBrief password",
            body=(
                _p("We received a request to reset the password for your SelfBrief account.")
                + _p("Click the button below to choose a new password.")
                + f"<p style=\"margin: 0 0 16px;\">{_cta(reset_link, 'Reset Password')}</p>"
                + '<p style="border-bottom: 1px solid #e8e8e8; margin: 24px 0;"></p>'
                + _p(
                    "This link expires in 1 hour. If you did not request a password reset, you can safely ignore this email."
                )
                + _p(f"Or copy this URL into your browser:<br>{link}")
            ),
        )
        logger.info("Password reset email sent to %s", to_email)
        return True
    except Exception as exc:
        logger.error("Failed to send password reset email to %s: %s", to_email, exc)
        return False
