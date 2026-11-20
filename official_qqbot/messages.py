"""一条消息怎么走：交互状态（点歌/GitHub 选择、模糊指令确认）→ 指令 → 天气 → 链接 → 生图 → 模糊纠正 → AI 聊天。"""

import math
import re
import time as _time_mod
from difflib import SequenceMatcher

from commands import _fuzzy_waiting, _github_select, _music_select, _music_waiting, handle_command
from config import IMAGE_AI_COOLDOWN_SECONDS, IMAGE_AI_ENABLED, _log, ai_chat, image_generator
from control_state import _log_command_event, _record_seen_user, _state_backend
from handlers import bilibili, douyin, fun, github, music, web_crawler
from handlers.image_gen import extract_image_prompt, is_image_request
from qq_api import reply, reply_image, reply_plain

_image_cooldowns: dict[str, float] = {}

# 提示词（prompts/catgirl.txt）让模型用 [---SPLIT---] 把长回复分成几条；以前原样发出去，用户会看到这串字。
_AI_SPLIT_RE = re.compile(r"\s*\[-{2,}\s*SPLIT\s*-{2,}\]\s*", re.IGNORECASE)
_AI_MAX_PARTS = 3  # 一条消息被动回复次数有限，最多拆 3 条，多出的并进最后一条


def split_ai_reply(text: str) -> list[str]:
    parts = [part.strip() for part in _AI_SPLIT_RE.split(str(text or "")) if part.strip()]
    if not parts:
        return [str(text or "")]
    if len(parts) > _AI_MAX_PARTS:
        parts = parts[: _AI_MAX_PARTS - 1] + ["\n\n".join(parts[_AI_MAX_PARTS - 1:])]
    return parts


async def reply_ai(ctx, text: str) -> None:
    """AI 回复：按 [---SPLIT---] 拆成几条依次发；前几条不带按钮，最后一条和以前一样。"""
    parts = split_ai_reply(text)
    for index, part in enumerate(parts):
        if len(part) > 2000:
            part = part[:2000] + "\n...(内容过长已截断)"
        if index < len(parts) - 1:
            await reply_plain(ctx, part)
        else:
            await reply(ctx, part)

# ====== 模糊指令 ======
_KNOWN_COMMANDS = {
    "签到",
    "打卡",
    "运势",
    "抽签",
    "今日运势",
    "每日运势",
    "今日人品",
    "人品",
    "jrrp",
    "帮助",
    "help",
    "菜单",
    "清除记忆",
    "重置记忆",
    "清空记忆",
    "重置对话",
    "清空对话",
    "nfa",
    "4399",
    "sauth",
    "163",
    "stock",
    "bind",
    "绑定邮箱",
    "解绑邮箱",
    "bjd",
    "布吉岛",
    "hypban",
    "status",
    "datalog",
    "更新日志",
    "排行榜",
    "积分榜",
    "签到排行",
    "积分",
    "余额",
    "查积分",
    "点歌",
    "听歌",
    "来首歌",
    "/nfa",
    "/4399",
    "/sauth",
    "/163",
    "/stock",
    "/bind",
    "/unbind",
    "/绑定邮箱",
    "/解绑邮箱",
    "/bjd",
    "/hypban",
    "/status",
    "/datalog",
    "/jrrp",
    "/ad",
}

_FUZZY_SPECS = [
    ("nfa", "获取 NFA Token"),
    ("4399", "获取 4399 账号密码"),
    ("sauth", "只获取 4399 Sauth"),
    ("163", "领取 163 小号"),
    ("stock", "查看全部库存"),
    ("/bind", "绑定资源接收邮箱"),
    ("bjd", "查询布吉岛版本"),
    ("hypban", "查询 Hypixel 封禁"),
    ("status", "查看运行状态"),
    ("datalog", "查看更新日志"),
    ("签到", "每日签到"),
    ("积分", "查询积分余额"),
    ("运势", "查看今日运势"),
    ("今日人品", "查看人品值"),
    ("帮助", "查看功能列表"),
    ("清除记忆", "重置 AI 对话"),
    ("排行榜", "查看签到排名"),
    ("点歌", "搜索歌曲"),
    ("/ad", "广告管理"),
]



def _find_fuzzy(content):
    best, best_score = None, 0
    for cmd, desc in _FUZZY_SPECS:
        score = SequenceMatcher(None, content.lower(), cmd.lower()).ratio()
        if score > best_score:
            best_score = score
            best = (cmd, desc)
    if best_score >= 0.55 and best:
        return best
    return None


# ====== 链接解析 ======
async def check_links(ctx, content):
    """检查消息中的 B站/抖音/网页链接"""
    # 抖音
    dy_url = douyin.extract_douyin_url(content)
    if dy_url:
        info = await douyin.get_video_info(dy_url)
        if info:
            await reply(ctx, info.get("text", "解析失败"))
        return True

    # B站
    bili_id = bilibili.extract_bilibili_id(content)
    if bili_id:
        info = await bilibili.get_video_info(bili_id)
        if info:
            await reply(ctx, info.get("text", "解析失败"))
        return True

    # 网页链接
    url = web_crawler.extract_url(content)
    if url:
        await reply(ctx, f"检测到链接，正在分析喵...\n{url}")
        try:
            ok, html = await web_crawler.fetch_page(url)
            if ok:
                page = web_crawler.extract_content(html, url)
                title = page.get("title", "")
                desc = page.get("description", "")
                text = page.get("text", "")[:500]
                summary = f"网页标题：{title}\n描述：{desc}\n\n内容摘要：\n{text}"
                chat_id = f"web_{ctx['user_openid']}"
                ai_reply = await ai_chat.chat(
                    chat_id, f"请分析这个网页的内容：\n{summary}"
                )
                await reply_ai(ctx, ai_reply)
            else:
                await reply(ctx, f"网页抓取失败喵：{html}")
        except Exception as e:
            await reply(ctx, f"网页分析出错喵：{e}")
        return True

    return False


# ====== 天气 ======
async def check_weather(ctx, content):
    match = re.match(r"^(.{1,10}?)天气$", content)
    if match:
        city = match.group(1)
        try:
            result = await fun.get_weather(city)
            await reply(ctx, result)
        except Exception:
            await reply(ctx, f"获取 {city} 天气失败喵~")
        return True
    return False


async def handle_image_request(ctx, content):
    if not is_image_request(content):
        return False

    prompt = extract_image_prompt(content)
    if not prompt:
        await reply(ctx, "请在 /生图 后面填写图片描述\n例如：/生图 雨夜里的重庆")
        return True
    if not IMAGE_AI_ENABLED:
        await reply(ctx, "AI 生图尚未启用，请联系管理员")
        return True
    if not image_generator.configured:
        await reply(ctx, "AI 生图尚未配置，请联系管理员")
        return True

    user_key = str(ctx.get("limit_user_id") or ctx["user_openid"])
    now = _time_mod.monotonic()
    ready_at = _image_cooldowns.get(user_key, 0) + IMAGE_AI_COOLDOWN_SECONDS
    if now < ready_at:
        remaining = math.ceil(ready_at - now)
        await reply(ctx, f"AI 生图冷却中，还需 {remaining} 秒")
        return True

    _image_cooldowns[user_key] = now
    await reply(ctx, f"## AI 生图\n\n正在生成：**{prompt}**\n\n请稍候……")
    image, error = await image_generator.generate(prompt)
    if not image:
        await reply(ctx, error or "图片生成失败，请稍后再试")
        return True
    if not await reply_image(ctx, image):
        await reply(ctx, "图片已经生成，但发送到 QQ 失败，请稍后重试")
    return True


# ====== 消息入口 ======
def _pending_for_context(states, ctx):
    pending = states.get(ctx["user_openid"])
    if not pending:
        return None
    origin = pending.get("ctx", {})
    if (origin.get("type"), origin.get("group_openid", "")) != (
        ctx.get("type"), ctx.get("group_openid", "")
    ):
        return None
    return pending


def _is_group_command(ctx, content):
    """Only explicit commands and same-conversation follow-ups trigger full events."""
    content = content.strip()
    if not content:
        return False
    lower = content.lower()
    if lower.startswith("/") or lower in _KNOWN_COMMANDS or lower in {
        "mdtest", "auth", "quit", "admin", "whois", "unbind", "绑邮箱", "取消邮箱", "查库存",
    }:
        return True
    if re.match(
        r"^(?:auth|点歌|听歌|来首歌|搜索github|github搜|搜索gh|绑定邮箱|绑定qq号)\s+",
        content, re.IGNORECASE,
    ) or re.fullmatch(r".{1,10}天气", content):
        return True
    if re.match(r"^(?:生图|绘图)(?:\s|$)", content):
        return True
    now = _time_mod.time()
    for states in (_music_select, _github_select):
        pending = _pending_for_context(states, ctx)
        if pending and now - pending.get("ts", 0) < 120 and content.isdigit():
            return True
    pending = _pending_for_context(_music_waiting, ctx)
    if pending and now - pending.get("ts", 0) < 60:
        return True
    pending = _pending_for_context(_fuzzy_waiting, ctx)
    return bool(pending and now < pending.get("expire", 0) and lower in {"y", "n", "no", "取消", "算了"})


async def process_message(ctx, content):
    """统一消息处理入口：先异步取一次发消息用户的控制台状态，本条消息内复用。"""
    prefetch = getattr(_state_backend, "prefetch_user_state", None)
    token = await prefetch(ctx.get("user_openid", "")) if prefetch else None
    try:
        await _process_message(ctx, content)
    finally:
        end_message = getattr(_state_backend, "end_message", None)
        if end_message:
            end_message(token)


async def _process_message(ctx, content):
    image_urls = tuple(ctx.get("image_urls") or ())
    commands_only = bool(ctx.get("commands_only"))
    if commands_only and not _is_group_command(ctx, content):
        return
    if not content and not image_urls:
        await reply(ctx, "喵？你叫我了吗~")
        return

    user_id = ctx["user_openid"]
    _record_seen_user(ctx)
    now = _time_mod.time()

    # 0a. 点歌选择状态
    sel = _pending_for_context(_music_select, ctx)
    if sel and now - sel.get("ts", 0) < 120 and content.isdigit():
        idx = int(content)
        songs = sel.get("songs", [])
        if 1 <= idx <= len(songs):
            song = songs[idx - 1]
            del _music_select[user_id]
            share = f"歌曲：{song.get('name', '')}\n歌手：{song.get('artist', '')}\n链接：{song.get('url', '')}"
            await reply(ctx, share)
            return
        else:
            del _music_select[user_id]
            await reply(ctx, "序号不对哦，点歌已取消喵~")
            return

    # 0b. GitHub 选择状态
    gsel = _pending_for_context(_github_select, ctx)
    if gsel and now - gsel.get("ts", 0) < 120 and content.isdigit():
        idx = int(content)
        repos = gsel.get("repos", [])
        if 1 <= idx <= len(repos):
            repo = repos[idx - 1]
            del _github_select[user_id]
            detail = github.build_repo_detail(repo)
            await reply(ctx, detail)
            return
        else:
            del _github_select[user_id]
            await reply(ctx, "序号不对哦，搜索已取消喵~")
            return

    # 0c. 点歌等待输入歌名
    mw = _pending_for_context(_music_waiting, ctx)
    if mw and now - mw.get("ts", 0) < 60:
        del _music_waiting[user_id]
        songs = await music.search_music(content)
        if songs:
            lines = ["搜索到以下歌曲喵~回复序号选择："]
            for i, s in enumerate(songs[:5], 1):
                lines.append(f"{i}. {s.get('name', '')} - {s.get('artist', '')}")
            _music_select[user_id] = {"songs": songs[:5], "ctx": ctx, "ts": now}
            await reply_plain(ctx, "\n".join(lines))
        else:
            await reply(ctx, f"没有搜到「{content}」喵~")
        return
    elif mw:
        del _music_waiting[user_id]

    # 0d. 模糊指令确认
    fw = _pending_for_context(_fuzzy_waiting, ctx)
    if fw and now < fw.get("expire", 0):
        lowered = content.strip().lower()
        if lowered == "y":
            del _fuzzy_waiting[user_id]
            real_cmd = fw["command"]
            _log.info(f"[模糊指令] 确认执行: {real_cmd}")
            handled = await handle_command(ctx, real_cmd)
            if handled:
                _log_command_event(ctx, real_cmd)
            return
        if lowered in ("n", "no", "取消", "算了"):
            del _fuzzy_waiting[user_id]
            await reply(ctx, "好的喵~已取消")
            return
    if fw:
        del _fuzzy_waiting[user_id]

    # 1. 指令
    if await handle_command(ctx, content):
        _log_command_event(ctx, content)
        return

    # 2. 天气
    if await check_weather(ctx, content):
        return

    # 3. 链接解析
    if not commands_only and await check_links(ctx, content):
        return

    # 4. AI 生图
    if await handle_image_request(ctx, content):
        _log_command_event(ctx, content)
        return

    # 5. 模糊指令匹配（非已知指令才触发）
    if content.lower().strip() not in _KNOWN_COMMANDS:
        fuzzy = _find_fuzzy(content)
        if fuzzy:
            cmd, desc = fuzzy
            _fuzzy_waiting[user_id] = {"command": cmd, "ctx": ctx, "expire": now + 60}
            await reply_plain(
                ctx, f"你是不是想输入 {cmd} 呀？（{desc}）\n回复 y 确认执行喵~"
            )
            return

    # 6. AI 对话
    if commands_only:
        return
    chat_id = f"{ctx['type']}_{user_id}"
    ai_reply = await ai_chat.chat(chat_id, content, image_urls=image_urls)
    await reply_ai(ctx, ai_reply)
