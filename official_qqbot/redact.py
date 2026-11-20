"""日志脱敏：发号的账号、密码、Sauth、Token 不进发送日志、命令日志和 journal。

店主要求（2026-11-20）：「发号日志不要存账号密码」。
- redact_outbound(content)：发送日志的正文。发号消息整条换成「[发号] <类型>（已脱敏：账号密码不记录）」，
  成功还是失败看 status 列；其它带「密码：/Token：/Sauth：」的消息也整条换掉。
  control_api/redact_logs.py 用同一个函数处理已经存下的旧日志，所以新旧格式一致。
- redact_preview(text)：日志里的消息预览（用户发来的内容、命令日志），把标签后面的值和 /auth 口令换成 ***。
"""

from __future__ import annotations

import re

# 发号消息的开头（handlers/sauth.py、handlers/nfa.py 的格式）-> 类型
_RESOURCE_MARKERS = (
    ("主人您的4399账号来了喵", "4399 账号"),
    ("主人您的163小号来了喵", "163 小号"),
    ("主人您的nfa来了喵", "NFA"),
    ("4399 Sauth：", "4399 Sauth"),
)
_SECRET_LABELS = ("密码：", "密码:", "Token：", "Token:", "Sauth：", "Sauth:")

_PREVIEW_PATTERNS = (
    # 账号：xxx / 密码：xxx / Token：xxx / Sauth：xxx（中英文冒号），值到行尾或空白为止
    re.compile(r"((?:账号|密码|Token|token|Sauth|sauth|UUID)\s*[：:]\s*)\S+"),
    re.compile(r"((?:account|password|pwd)\s*[:=]\s*)\S+", re.IGNORECASE),
    # /auth 口令
    re.compile(r"^(\s*/?auth\s+)\S.*$", re.IGNORECASE | re.MULTILINE),
)


def resource_label(content: str) -> str | None:
    text = str(content or "")
    for marker, label in _RESOURCE_MARKERS:
        if marker in text:
            return label
    return None


def contains_secret(content: str) -> bool:
    text = str(content or "")
    return resource_label(text) is not None or any(label in text for label in _SECRET_LABELS)


def redact_outbound(content: str) -> str:
    """发送日志的正文：含账号密码的整条替换，其它原样。"""
    text = str(content or "")
    label = resource_label(text)
    if label:
        return f"[发号] {label}（已脱敏：账号密码不记录）"
    if any(secret in text for secret in _SECRET_LABELS):
        return "[已脱敏] 消息含账号密码，正文不记录"
    return text


def redact_preview(text: str) -> str:
    """日志预览：保留结构，只把敏感值换成 ***。"""
    result = str(text or "")
    for pattern in _PREVIEW_PATTERNS:
        result = pattern.sub(lambda m: m.group(1) + "***", result)
    return result
