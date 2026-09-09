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
    """Wrap email body with SelfBrief full-logo header on a dark brand bar."""
    if include_logo:
        brand = f"""
        <img src="cid:{_LOGO_CID}" alt="SelfBrief" height="40"
             style="display: block; height: 40px; width: auto; border: 0;" />
        """
    else:
        brand = """
        <span style="font-size: 20px; font-weight: 700; color: #ffffff;">SelfBrief</span>
        """

    return f"""
    <div style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; max-width: 560px; margin: 0 auto; color: #1a1a1a;">
      <div style="background: #1e2d40; padding: 28px 32px; border-radius: 8px 8px 0 0;">
        {brand}
      </div>
      <div style="background: #ffffff; padding: 32px 28px; border: 1px solid #e5e7eb; border-top: 0; border-radius: 0 0 8px 8px;">
        {body}
      </div>
    </div>
    """


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

    try:
        _send(
            to_email=to_email,
            subject=f"You've been invited to {org_name} on SelfBrief",
            body=f"""
      <h1 style="font-size: 22px; font-weight: 700; margin: 0 0 8px;">You've been invited to {org_name}</h1>
      <p style="font-size: 15px; color: #555; margin: 0 0 32px; line-height: 1.6;">
        A SelfBrief administrator has invited you to join <strong>{org_name}</strong>. Click the button below to set your password and access the platform.
      </p>

      <a href="{invite_link}"
         style="display: inline-block; background: #f97316; color: #fff; font-weight: 600; font-size: 14px; padding: 12px 28px; border-radius: 6px; text-decoration: none;">
        Accept Invite &amp; Set Password
      </a>

      <p style="font-size: 12px; color: #999; margin: 32px 0 0; line-height: 1.6;">
        This link expires after use. If you weren't expecting this invitation, you can ignore this email.
        <br/><br/>
        Or copy this URL into your browser:<br/>
        <span style="color: #555;">{invite_link}</span>
      </p>
            """,
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
            body=f"""
      <h1 style="font-size: 22px; font-weight: 700; margin: 0 0 12px;">Categorisation started</h1>
      <p style="font-size: 15px; color: #555; margin: 0 0 8px; line-height: 1.6;">
        The Categorisation job titled <strong>{title}</strong> for airport <strong>{airport}</strong> has been submitted and categorisation is now in progress.
      </p>
      <p style="font-size: 15px; color: #555; margin: 0; line-height: 1.6;">
        We will send you another email once it is complete.
      </p>
      <p style="font-size: 12px; color: #999; margin: 40px 0 0;">You're receiving this because you created this job on SelfBrief.</p>
            """,
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
            body=f"""
      <div style="display: inline-block; background: #dcfce7; color: #166534; font-size: 13px; font-weight: 600; padding: 4px 10px; border-radius: 99px; margin-bottom: 16px;">
        Completed
      </div>
      <h1 style="font-size: 22px; font-weight: 700; margin: 0 0 12px;">Categorisation complete</h1>
      <p style="font-size: 15px; color: #555; margin: 0; line-height: 1.6;">
        The Categorisation job titled <strong>{title}</strong> for airport <strong>{airport}</strong> has completed successfully. Log in to SelfBrief to view the results.
      </p>
      <p style="font-size: 12px; color: #999; margin: 40px 0 0;">You're receiving this because you created this job on SelfBrief.</p>
            """,
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
            body=f"""
      <div style="display: inline-block; background: #fee2e2; color: #991b1b; font-size: 13px; font-weight: 600; padding: 4px 10px; border-radius: 99px; margin-bottom: 16px;">
        Failed
      </div>
      <h1 style="font-size: 22px; font-weight: 700; margin: 0 0 12px;">Categorisation failed</h1>
      <p style="font-size: 15px; color: #555; margin: 0; line-height: 1.6;">
        The Categorisation job titled <strong>{title}</strong> for airport <strong>{airport}</strong> could not be completed.
      </p>
      <p style="font-size: 12px; color: #999; margin: 40px 0 0;">You're receiving this because you created this job on SelfBrief.</p>
            """,
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

    try:
        _send(
            to_email=to_email,
            subject="Reset your SelfBrief password",
            body=f"""
      <h1 style="font-size: 22px; font-weight: 700; margin: 0 0 8px;">Reset your password</h1>
      <p style="font-size: 15px; color: #555; margin: 0 0 32px; line-height: 1.6;">
        We received a request to reset the password for your SelfBrief account. Click the button below to choose a new password.
      </p>

      <a href="{reset_link}"
         style="display: inline-block; background: #f97316; color: #fff; font-weight: 600; font-size: 14px; padding: 12px 28px; border-radius: 6px; text-decoration: none;">
        Reset Password
      </a>

      <p style="font-size: 12px; color: #999; margin: 32px 0 0; line-height: 1.6;">
        This link expires in 1 hour. If you didn't request a password reset, you can safely ignore this email.
        <br/><br/>
        Or copy this URL into your browser:<br/>
        <span style="color: #555;">{reset_link}</span>
      </p>
            """,
        )
        logger.info("Password reset email sent to %s", to_email)
        return True
    except Exception as exc:
        logger.error("Failed to send password reset email to %s: %s", to_email, exc)
        return False
