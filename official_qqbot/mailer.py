"""邮件：发结果邮件（SMTP 在线程里跑）和邮箱地址小工具。"""

import re

from config import EMAIL_CONFIG
from handlers import email_sender

_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
_QQ_NUMBER_RE = re.compile(r"^[1-9]\d{4,11}$")


async def _send_result_email(to_addr: str, subject: str, body: str) -> tuple[bool, str]:
    """通过配置的 SMTP 发送邮件"""
    return await email_sender.send_email(
        smtp_host=EMAIL_CONFIG.get("smtp_host", ""),
        smtp_port=EMAIL_CONFIG.get("smtp_port", 465),
        smtp_user=EMAIL_CONFIG.get("smtp_user", ""),
        smtp_pass=EMAIL_CONFIG.get("smtp_pass", ""),
        from_addr=EMAIL_CONFIG.get("from_addr", ""),
        to_addr=to_addr,
        subject=subject,
        body=body,
        use_tls=EMAIL_CONFIG.get("use_tls", True),
        from_name=EMAIL_CONFIG.get("from_name", "Miracle Team"),
    )


def _normalize_email_addr(email: str) -> str:
    return (email or "").strip()


def _is_valid_email_addr(email: str) -> bool:
    return bool(_EMAIL_RE.match(_normalize_email_addr(email)))


def _qq_number_to_email(value: str) -> str:
    qq_number = (value or "").strip()
    if not _QQ_NUMBER_RE.match(qq_number):
        return ""
    return f"{qq_number}@qq.com"


def _mask_email_addr(email: str) -> str:
    if "@" not in email:
        return email
    local, domain = email.split("@", 1)
    if len(local) <= 2:
        masked_local = local[:1] + "*"
    else:
        masked_local = f"{local[:2]}***{local[-1]}"
    return f"{masked_local}@{domain}"
