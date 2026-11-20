"""管理类指令：/auth /quit /admin /restrict /resetlimit /ban /unban /addstaff /deletestaff /unbindqq /bindqq /ad。

每个函数处理一条指令，参数 c 是 commands.CommandContext；commands.COMMANDS 决定匹配顺序。
"""

import hmac
import re
import secrets
import time as _time_mod
from datetime import datetime

import ads as ads_store
from accounts import bind_qq_account, qq_bind_of, unbind_qq_account
from config import ADMIN_SECRET, MARKDOWN_ENABLED, QQ_BIND_VERIFY, _log
from control_state import _is_admin, _is_admin_or_staff, _log_email_outbound, _state_backend
from mailer import _mask_email_addr, _send_result_email
from qq_api import reply, reply_plain
from resources import _parse_restrict_command

# /ad 查看状态 {user_openid: expire_timestamp}
_ad_waiting: dict[str, float] = {}

BINDQQ_PREFIX_RE = r"^(?:/bindqq|绑定积分QQ|绑定积分qq|绑定QQ号|绑定qq号)"


# ====== /bindqq 本人验证 ======
# QQ 官方机器人只拿得到 openid，拿不到 QQ 号，没法直接确认「这个 QQ 号是不是你」。
# 打开 QQ_BIND_VERIFY=email 后：先往 QQ号@qq.com 发 6 位验证码，本人回 /bindqq QQ号 验证码 才绑定。
_QQ_BIND_CODE_TTL = 600
_QQ_BIND_RESEND_SECONDS = 60
_QQ_BIND_MAX_TRIES = 5
_qq_bind_codes: dict[str, dict] = {}


def _qq_bind_verification_required(limit_user_id: str, qq: str) -> bool:
    return QQ_BIND_VERIFY == "email" and qq_bind_of(limit_user_id) != qq


async def _start_qq_bind_verification(ctx, limit_user_id: str, qq: str) -> str:
    now = _time_mod.time()
    to_addr = f"{qq}@qq.com"
    masked_addr = _mask_email_addr(to_addr)
    # 星号是字面文本，不是 Markdown 强调
    display_addr = masked_addr.replace("*", r"\*") if MARKDOWN_ENABLED else masked_addr
    pending = _qq_bind_codes.get(limit_user_id)
    # 每人每 60 秒最多发一封，不管填的是哪个 QQ 号（防止拿机器人给别人邮箱刷信）
    if pending and now - pending.get("sent_at", 0) < _QQ_BIND_RESEND_SECONDS:
        wait = int(_QQ_BIND_RESEND_SECONDS - (now - pending.get("sent_at", 0))) + 1
        if pending.get("qq") == qq:
            return (
                f"验证码已发到 QQ 邮箱 {display_addr}，请查收后发送：/bindqq {qq} 验证码\n"
                f"（{wait} 秒后可重新获取）"
            )
        return f"操作太频繁啦喵~请 {wait} 秒后再试"
    code = f"{secrets.randbelow(1_000_000):06d}"
    subject = "Miracle 积分账户绑定验证码"
    body = (
        f"你正在把 QQ 机器人的积分账户绑定到 QQ {qq}。\n\n"
        f"验证码：{code}\n\n"
        f"10 分钟内在机器人对话里发送：/bindqq {qq} {code}\n"
        "如果不是你本人操作，请忽略这封邮件。"
    )
    ok, err = await _send_result_email(to_addr, subject, body)
    _log_email_outbound(ctx, ctx.get("user_openid", ""), ok, subject, masked_addr)
    if not ok:
        _log.warning(f"[QQ绑定] 验证码邮件发送失败 -> {masked_addr}: {err}")
        return "验证码邮件发送失败了喵，请稍后再试~"
    _qq_bind_codes[limit_user_id] = {
        "qq": qq,
        "code": code,
        "sent_at": now,
        "expire": now + _QQ_BIND_CODE_TTL,
        "tries": 0,
    }
    _log.info(f"[QQ绑定] 已发验证码 {limit_user_id[:8]}... -> {masked_addr}")
    return (
        f"为确认 QQ {qq} 是你本人，验证码已发到 QQ 邮箱 {display_addr}\n"
        f"10 分钟内发送：/bindqq {qq} 验证码"
    )


def _confirm_qq_bind_code(limit_user_id: str, qq: str, code: str) -> tuple[bool, str]:
    now = _time_mod.time()
    pending = _qq_bind_codes.get(limit_user_id)
    if not pending or pending.get("qq") != qq or now > pending.get("expire", 0):
        _qq_bind_codes.pop(limit_user_id, None)
        return False, f"验证码已过期或还没获取，请先发送：/bindqq {qq}"
    pending["tries"] = int(pending.get("tries", 0)) + 1
    if not hmac.compare_digest(str(code).encode("utf-8"), str(pending["code"]).encode("utf-8")):
        if pending["tries"] >= _QQ_BIND_MAX_TRIES:
            _qq_bind_codes.pop(limit_user_id, None)
            return False, f"验证码错误次数太多，请重新发送：/bindqq {qq}"
        return False, "验证码不对喵~请检查后重试"
    _qq_bind_codes.pop(limit_user_id, None)
    return True, ""


async def cmd_auth_prompt(c):
    if c.ctx["type"] != "c2c":
        await reply(c.ctx, "请私聊我发送 /auth 验证码 来验证身份喵~")
    else:
        await reply(c.ctx, "请发送：/auth 验证码")


async def cmd_auth(c):
    ctx, content, user_id = c.ctx, c.content, c.user_id
    code = content.split(None, 1)[1].strip() if " " in content else ""
    if ctx["type"] != "c2c":
        await reply(ctx, "请私聊我验证喵~不要在群里发验证码！")
        return

    # admin 验证
    if ADMIN_SECRET and code and hmac.compare_digest(
        code.encode("utf-8"), ADMIN_SECRET.encode("utf-8")
    ):
        _state_backend.add_admin(user_id)
        await reply_plain(
            ctx, "验证成功喵~您已成为管理员！\n授权已持久化，重启后无需重新验证~"
        )
        _log.info(f"[Auth] {user_id[:8]}... 成为 admin")
        return

    # staff 密码验证
    if _state_backend.staff_exists(user_id):
        staff_password = _state_backend.get_staff_password(user_id)
        if not staff_password:
            _state_backend.set_staff_password(user_id, code)
            _state_backend.login_staff(user_id)
            await reply_plain(
                ctx, f"Staff 密码设置成功喵~已登陆！\n（密码：{code}）"
            )
            _log.info(f"[Staff] {user_id[:8]}... 激活")
            return
        if staff_password == code:
            _state_backend.login_staff(user_id)
            await reply_plain(ctx, "Staff 登陆成功喵~")
            _log.info(f"[Staff] {user_id[:8]}... 登陆")
            return

    await reply_plain(ctx, "验证码不正确喵~")


async def cmd_quit(c):
    removed = _state_backend.logout(c.user_id)
    await reply_plain(c.ctx, "已退出登陆喵~" if removed else "您当前没有登陆状态喵~")


async def cmd_admin_panel(c):
    if not _is_admin(c.user_id):
        await reply(c.ctx, "需要管理权限喵~请先私聊 /auth 验证码")
        return
    counts = _state_backend.counts()
    lines = [
        "曦曦官方Bot 管理面板",
        "━━━━━━━━━━━━━━",
        f"管理员：{counts['admins']} 人",
        f"Staff：{counts['staff']} 人（在线 {counts['staff_online']}）",
        f"封禁：{counts['banned']} 人",
        "━━━━━━━━━━━━━━",
        "可用指令：",
        "  /ban openid — 封禁用户",
        "  /unban openid — 解封用户",
        "  /addstaff openid — 添加 Staff",
        "  /deletestaff openid — 移除 Staff",
        "  /ad — 广告管理",
    ]
    await reply_plain(c.ctx, "\n".join(lines))


async def cmd_restrict(c):
    if not _is_admin_or_staff(c.user_id):
        await reply(c.ctx, "需要管理权限喵~")
        return
    parsed = _parse_restrict_command(c.content)
    if not parsed:
        await reply(
            c.ctx,
            "用法：/restrict 163/4399/nfa 数量 时间\n时间支持：min hour day month quarter year",
        )
        return
    feature, limit, unit = parsed
    _state_backend.set_resource_limit(feature, limit, unit, updated_by=c.user_id)
    await reply_plain(c.ctx, f"已设置 {feature} 每个用户 {unit} 内最多获取 {limit} 个")


async def cmd_reset_limit(c):
    if not _is_admin_or_staff(c.user_id):
        await reply(c.ctx, "需要管理权限喵~")
        return
    _state_backend.reset_resource_usage()
    await reply_plain(c.ctx, "已重置所有用户的获取限制记录，/restrict 规则保持不变")


async def cmd_ban(c):
    if not _is_admin_or_staff(c.user_id):
        await reply(c.ctx, "需要管理权限喵~")
        return
    target = c.content[5:].strip()
    if not target:
        await reply(c.ctx, "用法：/ban openid")
        return
    _state_backend.ban_user(target)
    await reply_plain(c.ctx, f"已封禁 {target[:12]}... 喵~")
    _log.info(f"[Ban] {c.user_id[:8]}... 封禁了 {target[:12]}...")


async def cmd_unban(c):
    if not _is_admin_or_staff(c.user_id):
        await reply(c.ctx, "需要管理权限喵~")
        return
    target = c.content[7:].strip()
    if not _state_backend.is_banned(target):
        await reply(c.ctx, "该用户不在封禁列表中喵~")
        return
    _state_backend.unban_user(target)
    await reply_plain(c.ctx, f"已解封 {target[:12]}... 喵~")
    _log.info(f"[Unban] {c.user_id[:8]}... 解封了 {target[:12]}...")


async def cmd_add_staff(c):
    if not _is_admin(c.user_id):
        await reply(c.ctx, "仅 Admin 可添加 Staff 喵~")
        return
    target = c.content[10:].strip()
    if not target:
        await reply(c.ctx, "用法：/addstaff openid")
        return
    if _state_backend.staff_exists(target):
        await reply(c.ctx, "该用户已是 Staff 喵~")
        return
    _state_backend.add_staff(target, added_by=c.user_id[:12])
    await reply_plain(
        c.ctx, f"已添加 Staff {target[:12]}... 喵~\n对方需私聊 /auth 密码 激活"
    )
    _log.info(f"[Staff] {c.user_id[:8]}... 添加了 {target[:12]}...")


async def cmd_delete_staff(c):
    if not _is_admin(c.user_id):
        await reply(c.ctx, "仅 Admin 可移除 Staff 喵~")
        return
    target = c.content[13:].strip()
    if not _state_backend.staff_exists(target):
        await reply(c.ctx, "该用户不是 Staff 喵~")
        return
    _state_backend.delete_staff(target)
    await reply_plain(c.ctx, f"已移除 Staff {target[:12]}... 喵~")
    _log.info(f"[Staff] {c.user_id[:8]}... 移除了 {target[:12]}...")


async def cmd_bindqq(c):
    """/bindqq QQ号 [验证码]：统一积分账户"""
    ctx = c.ctx
    bindqq_match = re.match(BINDQQ_PREFIX_RE + r"\s+(\d{5,11})(?:\s+(\d{6}))?$", c.content)
    if not bindqq_match:
        await reply(
            ctx,
            "发送 /bindqq 你的QQ号，把所有群的积分合并成一个账户喵~\n"
            "例如：/bindqq 123456789\n换群、私聊积分都算同一个账户！",
        )
        return
    qq, code = bindqq_match.group(1), bindqq_match.group(2)
    if _qq_bind_verification_required(c.limit_user_id, qq):
        if not code:
            await reply_plain(ctx, await _start_qq_bind_verification(ctx, c.limit_user_id, qq))
            return
        verified, error = _confirm_qq_bind_code(c.limit_user_id, qq, code)
        if not verified:
            await reply_plain(ctx, error)
            return
    ok, message = bind_qq_account(c.limit_user_id, qq)
    await reply_plain(ctx, message)


async def cmd_unbindqq(c):
    """/unbindqq openid（管理员解绑统一账户）"""
    if not _is_admin(c.user_id):
        await reply(c.ctx, "仅 Admin 可解绑 QQ 统一账户喵~")
        return
    target = c.content[len("/unbindqq "):].strip()
    if not unbind_qq_account(target):
        await reply(c.ctx, "该账号没有绑定 QQ 统一账户喵~")
        return
    await reply_plain(
        c.ctx,
        f"已解绑 {target[:12]}... 的 QQ 账户绑定喵~（余额留在 QQ 账户中不变）",
    )
    _log.info(f"[QQ绑定] 管理员解绑 {target[:12]}...")


async def cmd_ads(c):
    """/ad 广告管理（/ad 列表、/ad+ 新增、/ad- 删除、/ads+ 展示、/ads- 停止展示）"""
    ctx, user_id = c.ctx, c.user_id
    if not _is_admin_or_staff(user_id):
        await reply(ctx, "需要管理权限喵~")
        return

    ads = ads_store._refresh_ads_from_file()
    action = c.match.group(1)
    payload = c.match.group(2).strip()

    if not action:
        _ad_waiting[user_id] = _time_mod.time() + 300
        lines = ["当前广告列表喵~", "━━━━━━━━━━━━━━"]
        for ad in ads:
            status = "展示中" if ad.get("enabled") else "未展示"
            lines.append(f"[{ad.get('id')}] {status}\n{ad.get('content', '')}")
        lines.append("━━━━━━━━━━━━━━")
        lines.append("用法：/ad+ 内容 | /ad- 编号 | /ads+ 编号 [时间] | /ads- 编号")
        await reply_plain(ctx, "\n".join(lines))
        return

    if action == "+":
        if not payload:
            await reply_plain(ctx, "用法：/ad+ 广告内容")
            return
        new_id = ads_store.next_id(ads)
        ads.append(
            {
                "id": new_id,
                "content": payload,
                "enabled": False,
                "active_until": None,
                "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }
        )
        ads_store._save_ads()
        await reply_plain(
            ctx,
            f"已新增广告 [{new_id}] 到列表喵~（未展示）\n使用 /ads+ {new_id} 开启展示",
        )
        return

    if action == "-":
        target = ads_store.find_ad(ads, payload)
        if not target:
            await reply_plain(ctx, "没有找到这个广告喵~请用编号")
            return
        ads.remove(target)
        ads_store._save_ads()
        await reply_plain(ctx, f"已删除广告 [{target.get('id')}] 喵~")
        return

    if action == "s+":
        if _ad_waiting.get(user_id, 0) <= _time_mod.time():
            await reply_plain(ctx, "请先输入 /ad 查看列表")
            return
        selector = payload.split()[0] if payload else ""
        target = ads_store.find_ad(ads, selector)
        if not target:
            await reply_plain(ctx, "没有找到这个广告喵~")
            return
        target["enabled"] = True
        # 解析时间
        time_part = (
            payload[len(selector):].strip() if len(payload) > len(selector) else ""
        )
        if time_part:
            m = re.fullmatch(r"(\d+)(m|h|d)", time_part.lower())
            if m:
                val = int(m.group(1))
                unit = m.group(2)
                secs = val * (60 if unit == "m" else 3600 if unit == "h" else 86400)
                target["active_until"] = _time_mod.time() + secs
        else:
            target["active_until"] = None
        ads_store._save_ads()
        await reply_plain(ctx, f"已开始展示广告 [{target.get('id')}] 喵~")
        return

    if action == "s-":
        if _ad_waiting.get(user_id, 0) <= _time_mod.time():
            await reply_plain(ctx, "请先输入 /ad 查看列表")
            return
        target = ads_store.find_ad(ads, payload)
        if not target:
            await reply_plain(ctx, "没有找到这个广告喵~请用编号")
            return
        target["enabled"] = False
        target["active_until"] = None
        ads_store._save_ads()
        await reply_plain(ctx, f"已移除展示广告 [{target.get('id')}] 喵~")
        return
