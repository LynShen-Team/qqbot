"""领资源：NFA、4399、sauth、163、库存，以及它们共用的限额、「处理中」标记和结果投递（邮件/私聊）。"""

import asyncio
from dataclasses import dataclass
from typing import Awaitable, Callable

import shared_cooldown as _shared_cd
from accounts import (
    EXTRACT_POINTS_COST,
    SAUTH_163_COOLDOWN,
    SAUTH_RATE_LIMIT,
    SAUTH_RATE_WINDOW,
    _account_label,
    _points_shortage_text,
    get_user_points,
    try_spend_points,
)
from ads import _append_full_ads_to_email
from config import MARKDOWN_ENABLED, NFA_PASSWORD, NFA_USERNAME, _log
from control_state import _log_email_outbound, _state_backend
from handlers import nfa, sauth
from mailer import _mask_email_addr, _send_result_email
from qq_api import reply

# ====== 冷却/频率常量 ======
NFA_COOLDOWN = 1800
_NFA_HOUR_LIMIT = 5
_NFA_BAN_DURATION = 86400

_RESTRICT_FEATURES = {"163", "4399", "nfa"}
_RESTRICT_UNITS = {"min", "hour", "day", "month", "quarter", "year"}
_active_resource_request_keys: set[str] = set()


def _get_bound_email(user_id: str) -> str:
    return _state_backend.get_email_binding(user_id)


async def _require_bound_email(ctx, user_id: str) -> bool:
    if _get_bound_email(user_id):
        return True
    await reply(
        ctx,
        "请先绑定邮箱再领取资源喵~\n输入：/bind 你的邮箱\n例如：/bind 123456@qq.com",
    )
    return False


def _resource_request_requires_email(ctx) -> bool:
    return ctx.get("type") != "c2c"


async def _send_resource_result(
    ctx,
    user_id: str,
    resource_key: str,
    resource_label: str,
    subject: str,
    result: str,
    quota_text: str = "",
) -> bool:
    to_addr = _get_bound_email(user_id)
    delivery_body = _append_quota_text(result, quota_text)

    if ctx.get("type") == "c2c":
        direct_body = _append_full_ads_to_email(delivery_body)
        ok = await reply(ctx, direct_body)
        if ok:
            return True
        if not to_addr:
            _log.warning(f"[{resource_key}] 私聊发送失败且未绑定邮箱 -> {user_id[:8]}...")
            return False

        ok, err = await _send_result_email(to_addr, subject, direct_body)
        masked_addr = _mask_email_addr(to_addr)
        _log_email_outbound(ctx, user_id, ok, subject, masked_addr)
        if ok:
            return True

        _log.warning(f"[{resource_key}] 私聊发送失败，邮箱兜底也失败 -> {masked_addr}: {err}")
        return False

    if not to_addr:
        await _require_bound_email(ctx, user_id)
        return False

    email_body = _append_full_ads_to_email(delivery_body)
    ok, err = await _send_result_email(to_addr, subject, email_body)
    masked_addr = _mask_email_addr(to_addr)
    _log_email_outbound(ctx, user_id, ok, subject, masked_addr)
    if ok:
        # The masking asterisks are literal text, not Markdown emphasis.
        display_addr = masked_addr.replace("*", r"\*") if MARKDOWN_ENABLED else masked_addr
        reply_text = f"{resource_label} 已发送到邮箱 {display_addr}，请查收喵~"
        if quota_text.strip():
            reply_text += f"\n{quota_text.strip()}"
        await reply(ctx, reply_text)
        return True

    _log.warning(f"[{resource_key}] 邮件发送失败 -> {masked_addr}: {err}")
    await reply(ctx, f"{resource_label} 邮件发送失败，请检查绑定邮箱或稍后再试喵~")
    return False


def _resource_request_key(feature: str, user_id: str, ctx: dict) -> str:
    chat_type = str(ctx.get("type") or "unknown").lower()
    chat_id = str(ctx.get("group_openid") or ctx.get("user_openid") or user_id)
    return f"{str(feature).lower()}:{chat_type}:{chat_id}:{user_id}"


def _begin_resource_request(feature: str, user_id: str, ctx: dict) -> bool:
    key = _resource_request_key(feature, user_id, ctx)
    if key in _active_resource_request_keys:
        return False
    _active_resource_request_keys.add(key)
    return True


def _end_resource_request(feature: str, user_id: str, ctx: dict) -> None:
    _active_resource_request_keys.discard(_resource_request_key(feature, user_id, ctx))


def _parse_restrict_command(content: str) -> tuple[str, int, str] | None:
    parts = content.strip().split()
    if len(parts) != 4 or parts[0].lower() != "/restrict":
        return None
    feature = parts[1].lower()
    unit = parts[3].lower()
    if feature not in _RESTRICT_FEATURES or unit not in _RESTRICT_UNITS:
        return None
    try:
        limit = int(parts[2])
    except ValueError:
        return None
    if limit <= 0:
        return None
    return feature, limit, unit


def _format_duration_cn(seconds: int) -> str:
    seconds = max(0, int(seconds or 0))
    if seconds <= 0:
        return "0s"
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    parts = []
    if hours:
        parts.append(f"{hours}h")
    if hours or minutes:
        parts.append(f"{minutes}min")
    parts.append(f"{seconds}s")
    return "".join(parts)


def _resource_quota_line(status: dict) -> str:
    limit = int(status.get("limit", 0) or 0)
    if limit <= 0:
        return ""
    count = int(status.get("count", 0) or 0)
    return f"当前获取：{count}/{limit}"


def _resource_quota_text(status: dict, include_reset: bool = False) -> str:
    lines = []
    quota_line = _resource_quota_line(status)
    if quota_line:
        lines.append(quota_line)
    reset_after = int(status.get("reset_after", 0) or 0)
    if include_reset and reset_after:
        lines.append(f"限额刷新：{_format_duration_cn(reset_after)}")
    return "\n".join(lines)


def _append_quota_text(body: str, quota_text: str = "") -> str:
    quota_text = str(quota_text or "").strip()
    if not quota_text:
        return body
    return body.rstrip() + "\n\n" + quota_text


async def _check_resource_restrict(ctx: dict, feature: str, user_id: str) -> dict | None:
    status = _state_backend.get_resource_limit_status(feature, user_id)
    if status.get("blocked"):
        quota_text = _resource_quota_text(status, include_reset=True)
        lines = ["已达到获取上限"]
        if quota_text:
            lines.append(quota_text)
        await reply(ctx, "\n".join(lines))
        return None
    return status


def _record_resource_restrict(feature: str, user_id: str) -> None:
    _state_backend.record_resource_usage(feature, user_id)


def _resource_success_quota_line(feature: str, user_id: str) -> str:
    status = _state_backend.get_resource_limit_status(feature, user_id)
    return _resource_quota_text(status, include_reset=True)


# ====== NFA ======
async def cmd_nfa(ctx, user_id: str, limit_user_id: str) -> None:
    if _resource_request_requires_email(ctx) and not await _require_bound_email(ctx, user_id):
        return
    if not await _check_resource_restrict(ctx, "nfa", limit_user_id):
        return

    banned, bh, bm = _shared_cd.is_banned("nfa", limit_user_id)
    if banned:
        await reply(
            ctx, f"您因疑似偷卡已被临时封禁，剩余 {bh}小时{bm}分钟 后解封喵~"
        )
        return
    in_cd, remain = _shared_cd.check_cooldown("nfa", limit_user_id, NFA_COOLDOWN)
    if in_cd:
        await reply(
            ctx, f"获取太频繁啦喵~请 {remain // 60}分{remain % 60}秒 后再试~"
        )
        return
    over, count = _shared_cd.check_hour_limit(
        "nfa", limit_user_id, _NFA_HOUR_LIMIT, _NFA_BAN_DURATION
    )
    if over:
        await reply(
            ctx, f"一小时内频繁获取NFA（{count}次），疑似偷卡，已封禁24小时喵~"
        )
        return
    _shared_cd.record_usage("nfa", limit_user_id)
    result = await nfa.get_nfa_token(NFA_USERNAME, NFA_PASSWORD)
    if "主人您的nfa来了喵" in result:
        _record_resource_restrict("nfa", limit_user_id)
        quota_text = _resource_success_quota_line("nfa", limit_user_id)
        result += "\n爱来自Miracle nfa bot喵~"
        await _send_resource_result(
            ctx,
            user_id,
            "NFA",
            "NFA",
            "Miracle NFA Token",
            result,
            quota_text=quota_text,
        )
    else:
        await reply(ctx, result)
    _log.info(f"[NFA] {user_id[:8]}...")


# ====== 4399 / sauth / 163：同一套流程，差别写在 ResourceSpec 里 ======
async def _gate_restrict(ctx, feature: str, account_id: str) -> bool:
    return await _check_resource_restrict(ctx, feature, account_id) is not None


async def _gate_sauth_rate(ctx, feature: str, account_id: str) -> bool:
    over, used, retry_after = _shared_cd.check_rate_limit(
        "sauth", account_id, SAUTH_RATE_LIMIT, SAUTH_RATE_WINDOW
    )
    if over:
        await reply(
            ctx,
            f"sauth 获取太频繁啦喵~1 分钟最多 {SAUTH_RATE_LIMIT} 次，"
            f"请 {max(retry_after, 1)} 秒后再试~"
        )
        return False
    return True


async def _gate_163_ban(ctx, feature: str, account_id: str) -> bool:
    banned, bh, bm = _shared_cd.is_banned("163", account_id)
    if banned:
        await reply(
            ctx, f"您因疑似偷卡已被临时封禁，剩余 {bh}小时{bm}分钟 后解封喵~"
        )
        return False
    return True


async def _gate_163_cooldown(ctx, feature: str, account_id: str) -> bool:
    in_cd, remain = _shared_cd.check_cooldown("163", account_id, SAUTH_163_COOLDOWN)
    if in_cd:
        await reply(
            ctx,
            f"163 小号每小时只能提取 1 次喵~请 {remain // 60}分{remain % 60}秒 后再试~",
        )
        return False
    return True


@dataclass(frozen=True)
class ResourceSpec:
    key: str  # "4399" / "sauth" / "163"：积分价格、冷却、用量记录都用它
    fetch: Callable[[], Awaitable[tuple[bool, str]]]
    label: str  # 回复里的名字
    subject: str  # 邮件标题
    gates: tuple = ()  # 按顺序检查，任何一个返回 False 就停
    suffix: str = ""  # 成功结果后面追加的一行
    log_tag: str = ""
    cooldown_after_success: bool = False  # 163：成功后写冷却 + 小时记录


RESOURCE_SPECS = {
    "4399": ResourceSpec(
        key="4399", fetch=lambda: sauth.get_4399_credentials(), label="4399 账号",
        subject="Miracle 4399 账号密码", gates=(_gate_restrict,),
        suffix="\n爱来自Miracle小号网站喵~", log_tag="4399",
    ),
    "sauth": ResourceSpec(
        key="sauth", fetch=lambda: sauth.get_sauth(), label="4399 Sauth",
        subject="Miracle 4399 Sauth", gates=(_gate_sauth_rate,), log_tag="Sauth",
    ),
    "163": ResourceSpec(
        key="163", fetch=lambda: sauth.get_163_credentials(), label="163 小号",
        subject="Miracle 163 小号", gates=(_gate_restrict, _gate_163_ban, _gate_163_cooldown),
        log_tag="163", cooldown_after_success=True,
    ),
}


async def cmd_extract(spec: ResourceSpec, ctx, user_id: str, limit_user_id: str, account_id: str) -> None:
    """扣积分领号：邮箱检查 → 各自的门槛 → 余额 → 「处理中」→ 取号 → 扣分、记用量、投递。"""
    if _resource_request_requires_email(ctx) and not await _require_bound_email(ctx, user_id):
        return
    for gate in spec.gates:
        if not await gate(ctx, spec.key, account_id):
            return

    cost = EXTRACT_POINTS_COST[spec.key]
    balance = get_user_points(account_id)
    if balance < cost:
        await reply(ctx, _points_shortage_text(cost, balance))
        return

    if not _begin_resource_request(spec.key, limit_user_id, ctx):
        _log.info(f"[ResourceDedup] {spec.key} busy user={limit_user_id[:8]}...")
        return

    try:
        success, result = await spec.fetch()
        if success:
            spent, remaining = try_spend_points(account_id, cost)
            if not spent:
                _log.warning(
                    f"[{spec.key}] 积分并发扣减失败 account={_account_label(account_id)} balance={remaining}"
                )
            if spec.cooldown_after_success:
                _shared_cd.record_usage(spec.key, account_id)
            _shared_cd.record_rate_usage(spec.key, account_id)
            _state_backend.record_resource_usage(spec.key, account_id, ctx=ctx)
            quota_text = f"已消耗积分：{cost}\n剩余积分：{remaining}"
            result += spec.suffix
            await _send_resource_result(
                ctx,
                user_id,
                spec.key,
                spec.label,
                spec.subject,
                result,
                quota_text=quota_text,
            )
        else:
            await reply(ctx, result)
        _log.info(f"[{spec.log_tag}] {user_id[:8]}...")
    finally:
        _end_resource_request(spec.key, limit_user_id, ctx)


# ====== 库存 ======
async def cmd_stock(ctx) -> None:
    lines = ["Miracle Bot 库存总览喵~", "━━━━━━━━━━━━━━"]
    # 三个库存接口互不依赖，并发查询（以前串行，合计 0.75–3 秒）
    nfa_stock, stock_4399, stock_163 = await asyncio.gather(
        nfa.get_nfa_stock(),
        sauth.get_4399_stock(),
        sauth.get_163_stock(),
        return_exceptions=True,
    )
    try:
        ok, count, _ = nfa_stock
        lines.append(f"NFA：{count}" if ok else "NFA：unavailable")
    except Exception:
        lines.append("NFA：unavailable")
    try:
        ok4, avail, total, _ = stock_4399
        lines.append(f"4399：{avail}/{total}" if ok4 else "4399：unavailable")
    except Exception:
        lines.append("4399：unavailable")
    try:
        ok163, avail163, total163, used163, _ = stock_163
        lines.append(
            f"163：{avail163}/{total163}" if ok163 else "163：unavailable"
        )
    except Exception:
        lines.append("163：unavailable")
    lines.append("━━━━━━━━━━━━━━")
    await reply(ctx, "\n".join(lines))
