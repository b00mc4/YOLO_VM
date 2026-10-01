from __future__ import annotations
import logging
from email.message import EmailMessage
import html
from time import monotonic
import aiosmtplib
from app.core.config import get_settings

settings = get_settings()
logger = logging.getLogger(__name__)

class _EmailServiceHealth:
    _DEGRADED_COOLDOWN_SECONDS = 60.0
    __slots__ = ("_degraded_until",)

    def __init__(self) -> None:
        self._degraded_until = 0.0

    def is_degraded(self) -> bool:
        return monotonic() < self._degraded_until

    def mark_failure(self) -> None:
        self._degraded_until = monotonic() + self._DEGRADED_COOLDOWN_SECONDS

    def mark_recovered(self) -> None:
        self._degraded_until = 0.0


_email_health = _EmailServiceHealth()


def is_email_service_degraded() -> bool:
    return _email_health.is_degraded()


def _render_email_html(heading: str, message_text: str, button_text: str, button_url: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="th">
  <body style="margin:0;padding:0;background-color:#f3f4f6;font-family:Segoe UI,Helvetica,Arial,sans-serif;">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background-color:#f3f4f6;padding:32px 16px;">
      <tr>
        <td align="center">
          <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:480px;background-color:#ffffff;border-radius:8px;overflow:hidden;">
            <tr>
              <td style="padding:32px 32px 24px 32px;">
                <h1 style="margin:0 0 16px 0;font-size:20px;color:#111827;">{heading}</h1>
                <p style="margin:0 0 24px 0;font-size:14px;line-height:1.6;color:#4b5563;">{message_text}</p>
                <table role="presentation" cellpadding="0" cellspacing="0">
                  <tr>
                    <td style="border-radius:6px;background-color:#2563eb;">
                      <a href="{button_url}" style="display:inline-block;padding:12px 24px;font-size:14px;font-weight:600;color:#ffffff;text-decoration:none;">{button_text}</a>
                    </td>
                  </tr>
                </table>
                <p style="margin:24px 0 0 0;font-size:12px;line-height:1.6;color:#9ca3af;">
                  หากปุ่มด้านบนไม่ทำงาน คัดลอกลิงก์นี้ไปวางในเบราว์เซอร์:<br>
                  <a href="{button_url}" style="color:#2563eb;word-break:break-all;">{button_url}</a>
                </p>
              </td>
            </tr>
          </table>
        </td>
      </tr>
    </table>
  </body>
</html>"""


async def _send_email(to_email: str, subject: str, text_body: str, html_body: str) -> None:
    message = EmailMessage()
    message["From"] = settings.smtp_from_email
    message["To"] = to_email
    message["Subject"] = subject
    message.set_content(text_body)
    message.add_alternative(html_body, subtype="html")

    try:
        async with aiosmtplib.SMTP(
            hostname=settings.smtp_host, port=settings.smtp_port
        ) as smtp:
            try:
                await smtp.starttls()
            except Exception as e:
                if "already using TLS" not in str(e):
                    raise
            await smtp.login(settings.smtp_user, settings.smtp_password)
            await smtp.send_message(message)
    except Exception:
        _email_health.mark_failure()
        raise
    else:
        _email_health.mark_recovered()

async def send_set_password_email(to_email: str, raw_token: str) -> None:
    link = f"{settings.frontend_url}/set-password?token={raw_token}"
    await _send_email(
        to_email=to_email,
        subject="ตั้งรหัสผ่านบัญชีของคุณ",
        text_body=f"กรุณาคลิกลิงก์เพื่อตั้งรหัสผ่าน: {link}",
        html_body=_render_email_html(
            heading="ตั้งรหัสผ่านบัญชีของคุณ",
            message_text="กรุณากดปุ่มด้านล่างเพื่อตั้งรหัสผ่านสำหรับบัญชีของคุณ",
            button_text="ตั้งรหัสผ่าน",
            button_url=link,
        ),
    )


async def send_invite_email(to_email: str, raw_token: str) -> None:
    link = f"{settings.frontend_url}/set-password?token={raw_token}"
    await _send_email(
        to_email=to_email,
        subject="ยินดีต้อนรับเข้าสู่ระบบ Village Guard",
        text_body=f"คุณได้รับเชิญให้เข้าใช้งานระบบ กรุณาคลิกลิงก์เพื่อตั้งรหัสผ่านและเริ่มใช้งานบัญชีของคุณ: {link}",
        html_body=_render_email_html(
            heading="ยินดีต้อนรับเข้าสู่ระบบ Village Guard",
            message_text="คุณได้รับเชิญให้เข้าใช้งานระบบ กรุณากดปุ่มด้านล่างเพื่อตั้งรหัสผ่านและเริ่มใช้งานบัญชีของคุณ",
            button_text="ตั้งรหัสผ่านและเริ่มใช้งาน",
            button_url=link,
        ),
    )

async def send_invite_email_background(to_email: str, raw_token: str) -> None:
    try:
        await send_invite_email(to_email, raw_token)
    except Exception:
        logger.warning("Failed to send invite email to %s", to_email)

async def send_set_password_email_background(to_email: str, raw_token: str) -> None:
    try:
        await send_set_password_email(to_email, raw_token)
    except Exception:
        logger.warning("Failed to send password reset email to %s", to_email)

async def send_bulk_plain_email(
    to_emails: list[str],
    subject: str,
    text_body: str,
    html_body: str,
) -> list[str]:
    if not to_emails:
        return []
    smtp = aiosmtplib.SMTP(hostname=settings.smtp_host, port=settings.smtp_port)
    try:
        await smtp.connect()
        try:
            await smtp.starttls()
        except aiosmtplib.errors.SMTPException as e:
            if "already using TLS" not in str(e):
                raise
        await smtp.login(settings.smtp_user, settings.smtp_password)

        failed_recipients: list[str] = []
        if to_emails:
            message = EmailMessage()
            message["From"] = settings.smtp_from_email
            message["Bcc"] = ", ".join(to_emails)
            message["Subject"] = subject
            message.set_content(text_body)
            message.add_alternative(html_body, subtype="html")

            try:
                await smtp.send_message(message)
            except Exception:
                logger.warning("Failed to send bulk email via BCC")
                failed_recipients.extend(to_emails)
    except Exception:
        _email_health.mark_failure()
        raise
    finally:
        try:
            await smtp.quit()
        except Exception as exc:
            logger.warning("Failed to quit smtp connection: %s", exc)

    _email_health.mark_recovered()
    return failed_recipients


def _render_blacklist_alert_html(
    camera_name: str, license_plate: str, province: str, detected_at_local: str
) -> str:
    return f"""<!DOCTYPE html>
<html lang="th">
  <body style="margin:0;padding:0;background-color:#f3f4f6;font-family:Segoe UI,Helvetica,Arial,sans-serif;">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background-color:#f3f4f6;padding:32px 16px;">
      <tr>
        <td align="center">
          <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:480px;background-color:#ffffff;border-radius:8px;overflow:hidden;">
            <tr>
              <td style="padding:32px;">
                <h1 style="margin:0 0 16px 0;font-size:20px;color:#b91c1c;">พบรถในบัญชีดำ</h1>
                <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="font-size:14px;color:#374151;">
                  <tr><td style="padding:4px 0;color:#6b7280;">ทะเบียน</td><td style="padding:4px 0;font-weight:600;">{html.escape(license_plate)}</td></tr>
                  <tr><td style="padding:4px 0;color:#6b7280;">จังหวัด</td><td style="padding:4px 0;font-weight:600;">{html.escape(province)}</td></tr>
                  <tr><td style="padding:4px 0;color:#6b7280;">กล้อง</td><td style="padding:4px 0;font-weight:600;">{html.escape(camera_name)}</td></tr>
                  <tr><td style="padding:4px 0;color:#6b7280;">เวลา</td><td style="padding:4px 0;font-weight:600;">{html.escape(detected_at_local)}</td></tr>
                </table>
              </td>
            </tr>
          </table>
        </td>
      </tr>
    </table>
  </body>
</html>"""


async def send_blacklist_alert_email(
    to_emails: list[str],
    camera_name: str,
    license_plate: str,
    province: str,
    detected_at_local: str,
) -> list[str]:
    subject = f"แจ้งเตือน: พบรถบัญชีดำ {license_plate} ({province})"
    text_body = (
        f"พบรถในบัญชีดำ\n"
        f"ทะเบียน: {license_plate}\n"
        f"จังหวัด: {province}\n"
        f"กล้อง: {camera_name}\n"
        f"เวลา: {detected_at_local}"
    )
    html_body = _render_blacklist_alert_html(camera_name, license_plate, province, detected_at_local)

    return await send_bulk_plain_email(to_emails, subject, text_body, html_body)

async def send_email_change_confirmation_email(to_email: str, raw_token: str) -> None:
    link = f"{settings.frontend_url}/confirm-email-change?token={raw_token}"
    await _send_email(
        to_email=to_email,
        subject="ยืนยันการเปลี่ยนอีเมลของบัญชีคุณ",
        text_body=f"กรุณาคลิกลิงก์เพื่อยืนยันการเปลี่ยนอีเมล: {link}",
        html_body=_render_email_html(
            heading="ยืนยันการเปลี่ยนอีเมลของบัญชีคุณ",
            message_text="กรุณากดปุ่มด้านล่างเพื่อยืนยันว่าต้องการเปลี่ยนอีเมลของบัญชีเป็นอีเมลนี้",
            button_text="ยืนยันการเปลี่ยนอีเมล",
            button_url=link,
        ),
    )


async def send_email_change_confirmation_background(to_email: str, raw_token: str) -> None:
    try:
        await send_email_change_confirmation_email(to_email, raw_token)
    except Exception:
        logger.warning("Failed to send email change confirmation to %s", to_email)