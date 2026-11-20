"""QQ 开放平台接口：access token、共用连接池、发消息（文本/Markdown/图片）、群信息、网关地址。"""

import asyncio
import json
import re
import time as _time_mod

import httpx

from config import API_BASE, APP_ID, APP_SECRET, AUTH_URL, MARKDOWN_ENABLED, _log
from control_state import _log_outbound_event
from handlers.image_gen import GeneratedImage

# ====== QQ OpenAPI 共用连接池 ======
# 以前每次调用 QQ 接口都新建一个 httpx 客户端、用完即关，
# 从美国机房到 QQ 接口每次多一次 TCP+TLS 握手（实测单次调用中位 707ms -> 共用连接 184ms）。
# 现在同一事件循环内按超时值共用一个客户端；出 async with 块时不关闭连接。
# QQ 接口空闲 75 秒断开 keep-alive，这里 60 秒先关，避免复用到对端刚关掉的连接。
_QQ_HTTP_LIMITS = httpx.Limits(max_connections=50, max_keepalive_connections=20, keepalive_expiry=60)
_qq_http_clients: dict = {}


def _qq_http_client(timeout: float) -> httpx.AsyncClient:
    loop = asyncio.get_running_loop()
    factory = httpx.AsyncClient  # 按调用时的类取值，测试里 patch 的替身类照样生效
    key = (loop, timeout, factory)
    client = _qq_http_clients.get(key)
    if client is None or getattr(client, "is_closed", False):
        for stale in [k for k in _qq_http_clients if k[0] is not loop]:
            _qq_http_clients.pop(stale, None)
        client = factory(timeout=timeout, limits=_QQ_HTTP_LIMITS)
        _qq_http_clients[key] = client
    return client


class _shared_qq_http:
    """和原来新建 httpx 客户端的 async with 写法一样用，但出块不关闭，连接留在池里复用。"""

    def __init__(self, timeout: float):
        self._timeout = timeout

    async def __aenter__(self) -> httpx.AsyncClient:
        return _qq_http_client(self._timeout)

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        return False


# ====== Access Token ======
_access_token: str = ""
_token_expire_at: float = 0


async def refresh_access_token():
    global _access_token, _token_expire_at
    async with _shared_qq_http(15) as client:
        resp = await client.post(
            AUTH_URL, json={"appId": APP_ID, "clientSecret": APP_SECRET}
        )
        data = resp.json()
        token = data.get("access_token")
        if not token:
            body = json.dumps(data, ensure_ascii=False)[:500]
            raise RuntimeError(
                f"Auth response missing access_token: status={resp.status_code} body={body}"
            )
        _access_token = token
        expires_in = int(data.get("expires_in", 7200))
        _token_expire_at = _time_mod.time() + expires_in - 60
        _log.info(f"[Auth] access_token 有效期 {expires_in}s")


async def get_auth_header():
    if _time_mod.time() >= _token_expire_at:
        await refresh_access_token()
    return _build_bot_auth_header()


def _build_bot_auth_header():
    return {
        "Authorization": f"QQBot {_access_token}",
        "X-Union-Appid": APP_ID,
        "Content-Type": "application/json",
    }


def _build_gateway_auth_header():
    return {
        "Authorization": f"QQBot {_access_token}",
        "X-Union-Appid": APP_ID,
        "Content-Type": "application/json",
    }


async def get_gateway_auth_header():
    if _time_mod.time() >= _token_expire_at:
        await refresh_access_token()
    return _build_gateway_auth_header()


# ====== 消息发送 ======
_msg_seq_counter: dict[str, int] = {}

# QQ 官方 API 禁止消息包含 URL 域名，需要脱敏
_URL_SANITIZE_DOMAINS = [
    ".com",
    ".cn",
    ".net",
    ".org",
    ".io",
    ".me",
    ".de",
    ".tv",
    ".cc",
    ".top",
    ".xyz",
    ".app",
    ".dev",
    ".lol",
    ".site",
    ".online",
    "http://",
    "https://",
    "www.",
]


def _sanitize_url(text: str) -> str:
    """把消息中的域名/URL脱敏，避免 QQ 官方 API 400 拒绝"""
    result = text
    for domain in _URL_SANITIZE_DOMAINS:
        if domain in result:
            safe = domain.replace(".", "。").replace("://", "ˊ//")
            result = result.replace(domain, safe)
    return result


def _next_msg_seq(key: str) -> int:
    _msg_seq_counter[key] = _msg_seq_counter.get(key, 0) + 1
    return _msg_seq_counter[key]


def _build_text_payload(content: str, msg_id: str, msg_seq: int) -> dict:
    return {
        "content": content,
        "msg_type": 0,
        "msg_id": msg_id,
        "msg_seq": msg_seq,
    }


def _build_markdown_payload(
    content: str, msg_id: str, msg_seq: int, keyboard: dict | None = None
) -> dict:
    payload = {
        "msg_type": 2,
        "markdown": {"content": _normalize_message_start(content)},
        "msg_id": msg_id,
        "msg_seq": msg_seq,
    }
    if keyboard:
        payload["keyboard"] = keyboard
    return payload


def _normalize_message_start(content: str) -> str:
    """Remove empty leading lines/BOM without damaging Markdown indentation."""
    content = content.replace("\r\n", "\n").replace("\r", "\n")
    content = re.sub(r"\A(?:(?:[^\S\n]|[\u200b\ufeff])*\n)+", "", content)
    return content.lstrip("\u200b\ufeff")


def _build_send_payload(
    content: str,
    msg_id: str,
    msg_seq: int,
    markdown: bool,
    keyboard: dict | None = None,
) -> dict:
    if markdown and MARKDOWN_ENABLED:
        return _build_markdown_payload(content, msg_id, msg_seq, keyboard)
    return _build_text_payload(content, msg_id, msg_seq)


def _command_button(
    button_id: str,
    label: str,
    command: str,
    style: int = 1,
) -> dict:
    return {
        "id": button_id,
        "render_data": {
            "label": label,
            "visited_label": label,
            "style": style,
        },
        "action": {
            "type": 2,
            "data": command,
            "enter": True,
            "reply": False,
            "permission": {"type": 2},
        },
    }


def _default_resource_keyboard() -> dict:
    return _build_inline_keyboard(
        [
            [
                _command_button("quick_4399", "获取4399", "/4399", 1),
                _command_button("quick_sauth", "获取Sauth", "/sauth", 1),
                _command_button("quick_163", "获取163", "/163", 1),
            ],
            [
                _command_button("quick_sign", "每日签到", "签到", 1),
                _command_button("quick_stock", "查询库存", "/查库存", 1),
                _command_button("quick_bind", "快捷绑定", "/bind", 1),
            ]
        ]
    )


def _build_inline_keyboard(rows: list[list[dict]]) -> dict:
    return {
        "content": {
            "rows": [{"buttons": row[:5]} for row in rows[:5] if row],
        }
    }


def _status_ok(status_code: int) -> bool:
    return status_code in (200, 201, 202, 204)


async def _post_with_markdown_fallback(
    client,
    url: str,
    headers: dict,
    key: str,
    content: str,
    msg_id: str,
    log_prefix: str,
    keyboard: dict | None = None,
) -> bool:
    content = _normalize_message_start(content)
    body = _build_send_payload(
        content, msg_id, _next_msg_seq(key), markdown=True, keyboard=keyboard
    )
    resp = await client.post(url, headers=headers, json=body)
    if _status_ok(resp.status_code):
        return True

    if MARKDOWN_ENABLED and body.get("msg_type") == 2:
        _log.warning(
            f"{log_prefix} Markdown失败 {resp.status_code}: {resp.text[:200]}，降级文本"
        )
        fallback_body = _build_send_payload(
            content, msg_id, _next_msg_seq(key), markdown=False
        )
        resp = await client.post(url, headers=headers, json=fallback_body)
        if _status_ok(resp.status_code):
            return True

    _log.warning(f"{log_prefix} 失败 {resp.status_code}: {resp.text[:200]}")
    return False


_TEXT_TARGETS = {
    "group": ("{base}/v2/groups/{target}/messages", "g_{target}_{msg_id}"),
    "c2c": ("{base}/v2/users/{target}/messages", "c_{target}_{msg_id}"),
}


async def _send_text(
    target_type: str,
    target_id: str,
    content: str,
    msg_id: str,
    *,
    keyboard: dict | None = None,
    default_keyboard: bool = True,
    log_prefix: str,
) -> bool:
    """发一条文本/Markdown 消息（以前群、私聊、不带广告三份几乎一样的代码合成这一份）。"""
    content = _sanitize_url(content)
    if len(content) > 2000:
        content = content[:2000] + "\n...(内容过长已截断)"
    if keyboard is None and default_keyboard:
        keyboard = _default_resource_keyboard()
    headers = await get_auth_header()
    url_tpl, key_tpl = _TEXT_TARGETS[target_type]
    url = url_tpl.format(base=API_BASE, target=target_id)
    key = key_tpl.format(target=target_id, msg_id=msg_id)
    try:
        async with _shared_qq_http(15) as client:
            return await _post_with_markdown_fallback(
                client, url, headers, key, content, msg_id, log_prefix, keyboard
            )
    except Exception as e:
        _log.error(f"{log_prefix}异常: {e}")
        return False


async def send_group_msg(group_openid, content, msg_id, keyboard: dict | None = None):
    return await _send_text(
        "group", group_openid, content, msg_id, keyboard=keyboard, log_prefix="[发送] 群消息"
    )


async def send_c2c_msg(user_openid, content, msg_id, keyboard: dict | None = None):
    return await _send_text(
        "c2c", user_openid, content, msg_id, keyboard=keyboard, log_prefix="[发送] 私聊"
    )


def _build_media_upload_payload(image: GeneratedImage) -> dict:
    payload = {"file_type": 1, "srv_send_msg": False}
    if image.url:
        payload["url"] = image.url
    elif image.b64_json:
        payload["file_data"] = image.b64_json
    else:
        raise ValueError("image has no content")
    return payload


def _build_media_message_payload(file_info: str, msg_id: str, msg_seq: int) -> dict:
    return {
        "msg_type": 7,
        "media": {"file_info": file_info},
        "msg_id": msg_id,
        "msg_seq": msg_seq,
    }


async def _send_image(
    *, target_type: str, target_id: str, image: GeneratedImage, msg_id: str
) -> bool:
    if target_type == "group":
        base_url = f"{API_BASE}/v2/groups/{target_id}"
        key = f"g_{target_id}_{msg_id}"
        log_prefix = "[AI生图] 群图片"
    else:
        base_url = f"{API_BASE}/v2/users/{target_id}"
        key = f"c_{target_id}_{msg_id}"
        log_prefix = "[AI生图] 私聊图片"

    try:
        upload_payload = _build_media_upload_payload(image)
    except ValueError:
        _log.warning("%s 缺少图片内容", log_prefix)
        return False

    headers = await get_auth_header()
    try:
        async with _shared_qq_http(60) as client:
            upload = await client.post(
                f"{base_url}/files", headers=headers, json=upload_payload
            )
            if not _status_ok(upload.status_code):
                _log.warning("%s 上传失败 HTTP %s", log_prefix, upload.status_code)
                return False
            try:
                upload_result = upload.json()
            except (TypeError, ValueError):
                _log.warning("%s 上传响应格式无效", log_prefix)
                return False
            file_info = (
                str(upload_result.get("file_info") or "").strip()
                if isinstance(upload_result, dict)
                else ""
            )
            if not file_info:
                _log.warning("%s 上传响应缺少 file_info", log_prefix)
                return False

            response = await client.post(
                f"{base_url}/messages",
                headers=headers,
                json=_build_media_message_payload(
                    file_info, msg_id, _next_msg_seq(key)
                ),
            )
            if not _status_ok(response.status_code):
                _log.warning("%s 发送失败 HTTP %s", log_prefix, response.status_code)
                return False
            return True
    except Exception as exc:
        _log.warning("%s 异常: %s", log_prefix, type(exc).__name__)
        return False


async def send_group_image(group_openid, image: GeneratedImage, msg_id):
    return await _send_image(
        target_type="group",
        target_id=group_openid,
        image=image,
        msg_id=msg_id,
    )


async def send_c2c_image(user_openid, image: GeneratedImage, msg_id):
    return await _send_image(
        target_type="c2c",
        target_id=user_openid,
        image=image,
        msg_id=msg_id,
    )


# ====== 统一回复 ======
async def reply(ctx, text):
    if ctx["type"] == "group":
        ok = await send_group_msg(ctx["group_openid"], text, ctx["msg_id"])
        _log_outbound_event(ctx, "group", ok, text)
    else:
        ok = await send_c2c_msg(ctx["user_openid"], text, ctx["msg_id"])
        _log_outbound_event(ctx, "c2c", ok, text)
    return ok


async def reply_image(ctx, image: GeneratedImage):
    if ctx["type"] == "group":
        ok = await send_group_image(ctx["group_openid"], image, ctx["msg_id"])
        channel = "group"
    else:
        ok = await send_c2c_image(ctx["user_openid"], image, ctx["msg_id"])
        channel = "c2c"
    _log_outbound_event(ctx, channel, ok, "[AI 生图图片]")
    return ok


async def reply_markdown_card(ctx, markdown: str, button_rows: list[list[dict]]):
    keyboard = _build_inline_keyboard(button_rows)
    if ctx["type"] == "group":
        ok = await send_group_msg(
            ctx["group_openid"], markdown, ctx["msg_id"], keyboard=keyboard
        )
        _log_outbound_event(ctx, "group", ok, markdown)
    else:
        ok = await send_c2c_msg(
            ctx["user_openid"], markdown, ctx["msg_id"], keyboard=keyboard
        )
        _log_outbound_event(ctx, "c2c", ok, markdown)
    return ok


async def reply_plain(ctx, text):
    """不带广告、不带默认按钮的回复"""
    target_type = "group" if ctx["type"] == "group" else "c2c"
    target_id = ctx["group_openid"] if target_type == "group" else ctx["user_openid"]
    ok = False
    try:
        ok = await _send_text(
            target_type, target_id, text, ctx["msg_id"], default_keyboard=False, log_prefix="[发送plain]"
        )
    finally:
        _log_outbound_event(ctx, target_type, ok, text)
    return ok


async def fetch_group_member_count(group_openid: str) -> int | None:
    """查询群人数，失败返回 None（官方 /v2/groups/{id}/info 接口）"""
    if not group_openid:
        return None
    headers = await get_auth_header()
    try:
        async with _shared_qq_http(15) as client:
            resp = await client.get(
                f"{API_BASE}/v2/groups/{group_openid}/info", headers=headers
            )
            if resp.status_code == 200:
                data = resp.json()
                return int(data.get("group_member_num", 0) or 0)
            _log.warning(
                f"[拉群奖励] 群信息获取失败 {resp.status_code}: {resp.text[:200]}"
            )
    except Exception as e:
        _log.error(f"[拉群奖励] 群信息获取异常: {e}")
    return None


async def get_gateway_url():
    headers = await get_gateway_auth_header()
    async with _shared_qq_http(15) as client:
        resp = await client.get(f"{API_BASE}/gateway/bot", headers=headers)
        if resp.status_code != 200:
            _log.warning(f"[Gateway] {resp.status_code}: {resp.text[:300]}")
            return ""
        url = resp.json().get("url", "")
        _log.info(f"[Gateway] {url}")
        return url
