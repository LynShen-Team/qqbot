"""指令分发：COMMANDS 表按顺序匹配，第一个匹配的处理（以前是 handle_command 里约 800 行的 if 链）。

顺序就是以前 if 链的顺序，不能随便调：例如「帮助」在封禁检查之前，封禁检查在其它指令之前。
处理函数返回 False 表示「其实没处理，继续往下匹配」（只有「点歌 歌名」在歌名为空时用到）。
"""

import re
import time as _time_mod
from dataclasses import dataclass, field
from functools import cached_property
from typing import Awaitable, Callable

import commands_admin as admin
import resources
from accounts import (
    _account_key,
    do_sign_in,
    leaderboard_text,
    points_balance_text,
)
from config import AI_CONFIG, BOT_BUILD_DATE, BOT_VERSION, SANDBOX, _start_time, ai_chat
from control_state import _is_banned, _state_backend
from handlers import bjd, fun, github, hypban, music
from mailer import _is_valid_email_addr, _normalize_email_addr, _qq_number_to_email
from qq_api import _command_button, reply, reply_markdown_card, reply_plain
from config import _log

# ====== 交互状态 ======
# 点歌等待 {user_openid: {"ts": timestamp, "ctx": ctx}}
_music_waiting: dict[str, dict] = {}
# 点歌选择 {user_openid: {"songs": [...], "ctx": ctx, "ts": timestamp}}
_music_select: dict[str, dict] = {}
# GitHub 选择 {user_openid: {"repos": [...], "ctx": ctx, "ts": timestamp}}
_github_select: dict[str, dict] = {}
# 模糊指令确认 {user_openid: {"command": str, "ctx": ctx, "expire": timestamp}}
_fuzzy_waiting: dict[str, dict] = {}


@dataclass
class CommandContext:
    ctx: dict
    content: str
    lower: str
    match: object = None
    user_id: str = field(init=False)
    limit_user_id: str = field(init=False)

    def __post_init__(self):
        self.user_id = self.ctx["user_openid"]
        self.limit_user_id = self.ctx.get("limit_user_id") or self.user_id

    @cached_property
    def account_id(self) -> str:
        # 积分账户键：QQ 绑定 > union 映射 > limit_user_id（见 accounts.py 顶部说明）
        return _account_key(self.limit_user_id)


@dataclass(frozen=True)
class Command:
    name: str
    match: Callable[[str, str], object]  # (content, lower) -> 真值（可以是 re.Match）
    run: Callable[[CommandContext], Awaitable[bool | None]]


def exact(*words):
    return lambda content, lower: lower in words


def startswith(prefix):
    return lambda content, lower: content.startswith(prefix)


def regex(pattern, flags=0):
    compiled = re.compile(pattern, flags)
    return lambda content, lower: compiled.match(content)


def always(content, lower):
    return True


# ====== 普通指令 ======
async def cmd_mdtest(c):
    ctx = c.ctx
    await reply_markdown_card(
        ctx,
        (
            "## Markdown 测试\n\n"
            "服务：**Official QQBot**\n"
            "这是一条 Markdown 卡片测试消息。\n"
            f"ID:{ctx.get('msg_id', '')}\n\n"
            "> 如果下方按钮能显示，说明 keyboard 也生效。"
        ),
        [
            [
                _command_button("mdtest_status", "查看状态", "/status", 1),
                _command_button("mdtest_stock", "查看库存", "/stock", 1),
                _command_button("mdtest_help", "帮助", "/help", 1),
            ]
        ],
    )


async def cmd_help(c):
    ctx = c.ctx
    await reply(
        ctx,
        (
            "Ciallo 曦曦官方Bot 指令列表喵~\n"
            "━━━ 日常功能 ━━━\n"
            "签到 / 打卡 — 每日签到 +50 积分\n"
            "积分 — 查询余额与积分规则\n"
            "/bindqq QQ号 — 统一积分账户（换群不丢积分）\n"
            "邀请我进群 — 10 人以上群 +25 积分（每日 10 次，拉群后在群里 @我 领取）\n"
            "排行榜 — 查看积分排名\n"
            "运势 / 抽签 — 查看今日运势\n"
            "今日人品 — 每日人品值\n"
            "XX天气 — 查询天气（如：北京天气）\n"
            "━━━ 点歌/搜索 ━━━\n"
            "点歌 歌名 — 搜索歌曲\n"
            "点歌 — 进入点歌模式，再输歌名\n"
            "搜索GitHub 关键词 — 搜索仓库\n"
            "━━━ 小号/资源（消耗积分） ━━━\n"
            "nfa — 获取 NFA Token\n"
            "4399 — 获取 4399 账号密码（2 积分，不限次）\n"
            "sauth — 只获取 4399 Sauth（5 积分，1 分钟最多 5 次）\n"
            "163 — 领取 163 小号（50 积分，1 小时最多 1 次）\n"
            "stock — 查看全部库存\n"
            "/bind 邮箱/QQ号 — 绑定资源接收地址（群聊领取前必需）\n"
            "/unbind — 取消邮箱绑定\n"
            "━━━ 查询功能 ━━━\n"
            "bjd — 查询布吉岛版本\n"
            "hypban — Hypixel 封禁统计\n"
            "status — 运行状态\n"
            "datalog — 完整更新日志\n"
            "/whois — 查看自己的 openid\n"
            "━━━ AI 对话 ━━━\n"
            "@我 + 任意内容 — AI 聊天\n"
            "/生图 描述 — 生成一张图片\n"
            "也可直接说：帮我画一张……\n"
            "清除记忆 — 重置对话上下文\n"
            "发送链接 — 自动网页分析\n"
            "发送B站/抖音链接 — 自动解析\n"
            "输错指令 — 智能纠正，回复 y 执行\n"
            "━━━ 管理指令（私聊） ━━━\n"
            "/auth 验证码 — 验证管理员身份\n"
            "/quit — 退出登陆\n"
            "/admin — 管理面板\n"
            "/ban openid — 封禁用户\n"
            "/unbindqq openid — 解绑QQ统一账户\n"
            "/unban openid — 解封用户\n"
            "/addstaff openid — 添加 Staff\n"
            "/deletestaff openid — 移除 Staff\n"
            "/ad — 广告管理\n"
            "/ad+ 内容 — 新增广告到列表\n"
            "/ad- 编号 — 删除广告\n"
            "/ads+ 编号 [时间] — 开启展示\n"
            "/ads- 编号 — 移除展示\n"
            "━━━━━━━━━━━━━━\n"
            f"v{BOT_VERSION} | 构建：{BOT_BUILD_DATE}\n"
            "群聊可直接发送指令；AI 聊天请 @我。私聊直接发消息即可喵~"
        ),
    )


async def ban_gate(c):
    """封禁的人到这里就停（帮助和 mdtest 之后、其它所有指令之前）。"""
    if _is_banned(c.user_id):
        await reply_plain(c.ctx, "您已被封禁，无法使用 Bot 喵~")
        return True
    return False


async def cmd_whois(c):
    email = _state_backend.get_email_binding(c.user_id) or "未绑定"
    await reply_plain(c.ctx, f"你的 openid：\n{c.user_id}\n绑定邮箱：{email}")


async def cmd_bind_email(c):
    ctx = c.ctx
    bind_target = c.match.group(1)
    email = _normalize_email_addr(bind_target)
    if ctx.get("type") == "c2c" and not _is_valid_email_addr(email):
        email = _qq_number_to_email(bind_target)
    if not _is_valid_email_addr(email):
        await reply(
            ctx,
            "格式不对喵~请输入：/bind 你的邮箱地址\n私聊也可以输入：/bind 你的QQ号\n例如：/bind 123456@qq.com",
        )
        return
    _state_backend.set_email_binding(c.user_id, email)
    await reply_plain(
        ctx, f"邮箱绑定成功喵~\n{email}\n之后领取 nfa/4399/163 会自动发到这个邮箱！"
    )
    _log.info(f"[邮箱] {c.user_id[:8]}... 绑定 {email}")


async def cmd_bind_usage(c):
    await reply(
        c.ctx,
        "请输入：/bind 你的邮箱地址\n私聊也可以输入：/bind 你的QQ号\n例如：/bind 123456@qq.com\n绑定后才能领取 nfa/4399/163 喵~",
    )


async def cmd_unbind_email(c):
    if _state_backend.delete_email_binding(c.user_id):
        await reply_plain(c.ctx, "已解绑邮箱喵~之后需要重新绑定邮箱才能领取资源。")
    else:
        await reply(c.ctx, "你还没有绑定邮箱喵~")


async def cmd_sign(c):
    # 签到（积分记到统一账户：QQ 绑定 > union > 场景 openid）
    await reply(c.ctx, do_sign_in(c.account_id))


async def cmd_points(c):
    await reply(c.ctx, points_balance_text(c.account_id))


async def cmd_fortune(c):
    await reply(c.ctx, fun.get_fortune())


async def cmd_jrrp(c):
    await reply(c.ctx, fun.get_jrrp(c.user_id, "你"))


async def cmd_clear_memory(c):
    chat_id = f"{c.ctx['type']}_{c.user_id}"
    ai_chat.clear_history(chat_id)
    await reply(c.ctx, "记忆已清除喵~我们重新开始吧！")


async def cmd_nfa(c):
    await resources.cmd_nfa(c.ctx, c.user_id, c.limit_user_id)


def extract(key):
    async def run(c):
        await resources.cmd_extract(
            resources.RESOURCE_SPECS[key], c.ctx, c.user_id, c.limit_user_id, c.account_id
        )
    return run


async def cmd_stock(c):
    await resources.cmd_stock(c.ctx)


async def cmd_bjd(c):
    info = await bjd.get_latest_version()
    await reply(
        c.ctx,
        bjd.build_update_msg(info, is_update=False)
        if info
        else "获取布吉岛版本失败喵~",
    )


async def cmd_hypban(c):
    await reply(c.ctx, await hypban.get_ban_stats())


async def cmd_status(c):
    ctx = c.ctx
    uptime = int(_time_mod.time() - _start_time)
    h, m, s = uptime // 3600, uptime % 3600 // 60, uptime % 60
    await reply(
        ctx,
        (
            f"曦曦官方Bot 运行状态\n"
            f"━━━━━━━━━━━━━━\n"
            f"版本：v{BOT_VERSION}\n"
            f"运行时间：{h}小时{m}分{s}秒\n"
            f"AI 模型：{AI_CONFIG.get('model', '?')}\n"
            f"环境：{'沙箱' if SANDBOX else '正式'}\n"
            f"构建日期：{BOT_BUILD_DATE}"
        ),
    )


async def cmd_datalog(c):
    ctx = c.ctx
    await reply_plain(
        ctx,
        (
            f"曦曦官方Bot 更新日志\n"
            f"━━━━━━━━━━━━━━━━━\n"
            f"v{BOT_VERSION} ({BOT_BUILD_DATE})\n"
            f"· 资源改为固定积分制：签到 +50/天，拉群 +25（10 人以上群，日上限 10 次）\n"
            f"· 提取消耗积分：4399=2（不限次）/ sauth=5（1 分钟 5 次）/ 163=50（1 小时 1 次）\n"
            f"· 新增「积分」指令查询余额\n"
            f"v1.2.0 (2026-10-05)\n"
            f"· 官方 Bot 上线，基于 QQ 开放平台 API\n"
            f"· AI 对话 + 签到/运势/人品/天气\n"
            f"· NFA/4399/163/库存查询\n"
            f"· 布吉岛/Hypban 查询\n"
            f"· B站/抖音/网页链接解析\n"
            f"· 点歌/GitHub 搜索\n"
            f"· 广告系统/模糊指令/排行榜\n"
            f"· 跨 Bot 共享冷却防偷卡\n"
            f"━━━━━━━━━━━━━━━━━"
        ),
    )


async def cmd_leaderboard(c):
    text = leaderboard_text()
    await reply(c.ctx, text if text else "还没有人签到过喵~")


async def cmd_song_search(c):
    ctx, content = c.ctx, c.content
    song_name = re.sub(r"^(点歌|听歌|来首歌)\s*[:：]?\s*", "", content).strip()
    if not song_name:
        return False  # 以前 if 链在这里会继续往下走
    songs = await music.search_music(song_name)
    if songs:
        lines = ["搜索到以下歌曲喵~回复序号选择："]
        for i, s in enumerate(songs[:5], 1):
            lines.append(f"{i}. {s.get('name', '')} - {s.get('artist', '')}")
        _music_select[c.user_id] = {
            "songs": songs[:5],
            "ctx": ctx,
            "ts": _time_mod.time(),
        }
        await reply_plain(ctx, "\n".join(lines))
    else:
        await reply(ctx, f"没有搜到「{song_name}」喵~")


async def cmd_song_wait(c):
    _music_waiting[c.user_id] = {"ts": _time_mod.time(), "ctx": c.ctx}
    await reply(c.ctx, "请输入歌名喵~（60秒内有效）")


async def cmd_github(c):
    ctx = c.ctx
    keyword = c.match.group(2).strip()
    repos = await github.search_repos(keyword)
    if repos:
        lines = [f"GitHub 搜索「{keyword}」结果喵~回复序号查看详情："]
        for i, r in enumerate(repos[:5], 1):
            lines.append(f"{i}. {r.get('full_name', '')} ⭐{r.get('stars', 0)}")
        _github_select[c.user_id] = {
            "repos": repos[:5],
            "ctx": ctx,
            "ts": _time_mod.time(),
        }
        await reply_plain(ctx, "\n".join(lines))
    else:
        await reply(ctx, f"没有搜到「{keyword}」相关仓库喵~")


_BIND_RE = r"^(?:/bind|绑定邮箱|/绑定邮箱|绑定QQ号|绑定qq号|/bindqq)"

# 顺序 = 以前 if 链的顺序
COMMANDS: list[Command] = [
    Command("mdtest", exact("/mdtest", "mdtest"), cmd_mdtest),
    Command("help", exact("/help", "help", "帮助", "菜单"), cmd_help),
    Command("ban-gate", always, ban_gate),
    Command("auth-prompt", exact("/auth", "auth"), admin.cmd_auth_prompt),
    Command("auth", lambda content, lower: content.startswith("/auth ") or content.startswith("auth "), admin.cmd_auth),
    Command("quit", exact("/quit", "quit"), admin.cmd_quit),
    Command("admin", exact("/admin", "admin"), admin.cmd_admin_panel),
    Command("restrict", lambda content, lower: lower.startswith("/restrict"), admin.cmd_restrict),
    Command("resetlimit", exact("/resetlimit"), admin.cmd_reset_limit),
    Command("ban", startswith("/ban "), admin.cmd_ban),
    Command("unban", startswith("/unban "), admin.cmd_unban),
    Command("addstaff", startswith("/addstaff "), admin.cmd_add_staff),
    Command("deletestaff", startswith("/deletestaff "), admin.cmd_delete_staff),
    Command("whois", exact("/whois", "whois"), cmd_whois),
    Command("bindqq", regex(admin.BINDQQ_PREFIX_RE + r"(?:\s|$)"), admin.cmd_bindqq),
    Command("unbindqq", startswith("/unbindqq "), admin.cmd_unbindqq),
    Command("bind-email", regex(_BIND_RE + r"\s+(\S+)$", re.IGNORECASE), cmd_bind_email),
    Command(
        "bind-usage",
        lambda content, lower: lower in ("/bind", "bind", "绑定邮箱", "/绑定邮箱", "绑邮箱")
        or re.match(_BIND_RE + r"\s+", content, re.IGNORECASE),
        cmd_bind_usage,
    ),
    Command("unbind-email", exact("/unbind", "unbind", "解绑邮箱", "/解绑邮箱", "取消邮箱"), cmd_unbind_email),
    Command("sign", exact("签到", "打卡"), cmd_sign),
    Command("points", exact("积分", "余额", "我的积分", "查积分", "points", "/points"), cmd_points),
    Command("fortune", exact("运势", "今日运势", "抽签", "每日运势"), cmd_fortune),
    Command("jrrp", exact("今日人品", "人品", "jrrp"), cmd_jrrp),
    Command("clear-memory", exact("清除记忆", "重置记忆", "清空记忆", "重置对话", "清空对话"), cmd_clear_memory),
    Command("nfa", exact("nfa", "/nfa"), cmd_nfa),
    Command("4399", exact("4399", "/4399"), extract("4399")),
    Command("sauth", exact("sauth", "/sauth"), extract("sauth")),
    Command("163", exact("163", "/163"), extract("163")),
    Command("stock", exact("stock", "/stock", "查库存", "/查库存"), cmd_stock),
    Command("bjd", exact("bjd", "/bjd", "布吉岛"), cmd_bjd),
    Command("hypban", exact("hypban", "/hypban"), cmd_hypban),
    Command("status", exact("status", "/status"), cmd_status),
    Command("datalog", exact("datalog", "/datalog", "更新日志"), cmd_datalog),
    Command("leaderboard", exact("排行榜", "积分榜", "签到排行"), cmd_leaderboard),
    Command("song-search", regex(r"^(点歌|听歌|来首歌)\s+.+"), cmd_song_search),
    Command("song-wait", exact("点歌", "听歌", "来首歌"), cmd_song_wait),
    Command("github", regex(r"^(搜索github|github搜|搜索gh)\s+(.+)", re.IGNORECASE), cmd_github),
    Command("ad", regex(r"^/ad(s?\+|s?-|)\s*(.*)", re.DOTALL), admin.cmd_ads),
]


async def handle_command(ctx, content):
    """处理所有指令，返回 True 表示已处理"""
    lower = content.lower().strip()
    for command in COMMANDS:
        matched = command.match(content or "", lower)
        if not matched:
            continue
        c = CommandContext(ctx, content, lower, matched)
        if await command.run(c) is False:
            continue
        return True
    return False
