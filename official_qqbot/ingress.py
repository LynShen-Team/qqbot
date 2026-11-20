"""QQ 事件入口：webhook（验签、先回包后处理）、WebSocket 网关、事件分发、群/私聊消息和拉群事件。

不要改的部分（见审查方案）：验签、WS 心跳和重连、adapters/qq_official.py。
"""

import asyncio
import json
import re
import time as _time_mod
from collections import OrderedDict
from dataclasses import replace as dataclasses_replace

import websockets
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric import ed25519

import qq_api
from accounts import (
    GROUP_ADD_MIN_MEMBERS,
    GROUP_ADD_REWARD_POINTS,
    SIGN_IN_POINTS,
    _account_key,
    _account_label,
    _queue_pending_reward,
    award_group_add_points,
    bot_mention_ids,
    get_user_points,
    is_known_member_openid,
    learn_bot_mention,
    notify_pending_group_reward,
    resolve_message_identity,
)
from adapters.qq_official import (
    C2C_MESSAGE_CREATE,
    GROUP_AT_MESSAGE_CREATE,
    GROUP_MESSAGE_EVENTS,
    adapt_message_event,
)
from config import (
    APP_ID,
    APP_SECRET,
    BRIDGE_HOST,
    BRIDGE_PORT,
    FULL_MESSAGE_GROUP_IDS,
    GATEWAY_INTENTS,
    QQ_WEBHOOK_ASYNC,
    QQ_WEBHOOK_PATH,
    _log,
)
from messages import _is_group_command, process_message
from redact import redact_preview
from qq_api import fetch_group_member_count, get_gateway_url, refresh_access_token, reply, send_group_msg

# ====== 收到的消息去重 ======
_INBOUND_GROUP_MSG_CACHE_MAX = 1000
_recent_group_msg_ids: OrderedDict[str, float] = OrderedDict()
_recent_c2c_msg_ids: OrderedDict[str, float] = OrderedDict()


def _remember_inbound_message(
    cache: OrderedDict[str, float], scope: str, msg_id: str
) -> bool:
    if not msg_id:
        return True

    key = f"{scope}:{msg_id}"
    if key in cache:
        return False

    cache[key] = _time_mod.time()
    while len(cache) > _INBOUND_GROUP_MSG_CACHE_MAX:
        cache.popitem(last=False)
    return True


def _remember_group_message(group_openid: str, msg_id: str) -> bool:
    return _remember_inbound_message(_recent_group_msg_ids, group_openid, msg_id)


def _remember_c2c_message(user_openid: str, msg_id: str) -> bool:
    return _remember_inbound_message(_recent_c2c_msg_ids, user_openid, msg_id)


# ====== 全量消息里的 @机器人 ======
_BOT_MENTION_RE = re.compile(r"^\s*<@!?([0-9A-Za-z]{4,32})>\s*")


def _bot_mention_ids() -> dict[str, str]:
    """每个群学到的 机器人被@ID（全量模式下 @机器人 用 32 位 bot openid，与成员同格式）"""
    return bot_mention_ids()


def _split_full_message_at_bot(content: str, group_openid: str = "") -> tuple[bool, str]:
    """
    全量消息开头的 @机器人 前缀识别。机器人的 @ID 与成员 openid 同为 32 位十六进制，
    无法靠格式区分，采用学习式：
    - 已学习的本群/跨群机器人 ID 或数字 appid → 是@我
    - 未知 ID + 斜杠指令（几乎必然发给机器人）→ 学习并视为@我
    - 已知是某成员的 openid → 不是@我
    返回 (是否@了机器人, 去掉前缀后的内容)
    """
    text = str(content or "")
    match = _BOT_MENTION_RE.match(text)
    if not match:
        return False, text
    token = match.group(1)
    rest = text[match.end():].strip()
    known_ids = set(_bot_mention_ids().values())
    if token and (token == str(APP_ID) or token in known_ids):
        return True, rest
    if rest.startswith("/") and token and not is_known_member_openid(token):
        learn_bot_mention(group_openid, token)
        _log.info(
            f"[全量@识别] 学习机器人@id group={group_openid[:8]}... id={token[:8]}..."
        )
        return True, rest
    _log.info(f"[全量@识别] 非机器人前缀 token={token[:8]}... rest={rest[:20]}")
    return False, text


# ====== 群 / 私聊 / 拉群 ======
async def handle_group_message(data, event_type=GROUP_AT_MESSAGE_CREATE):
    event = adapt_message_event(event_type, data, FULL_MESSAGE_GROUP_IDS)
    if not event:
        _log.debug(
            f"[群消息忽略] event={event_type} group={data.get('group_openid', '')}"
        )
        return

    # 全量模式下平台不再下发 GROUP_AT 事件，@机器人的消息
    # 以 GROUP_MESSAGE_CREATE 到达且内容带 <@id> 前缀：识别并按@消息处理
    if event.is_full_message and not event.is_at:
        _log.info(
            f"[全量消息] group={event.group_openid[:8]}... "
            f"user={event.user_openid[:8]}... raw={redact_preview(event.raw_content)[:80]!r}"
        )
        at_bot, stripped = _split_full_message_at_bot(
            event.content, event.group_openid
        )
        if at_bot:
            event = dataclasses_replace(event, content=stripped, is_at=True)

    # 身份学习与拉群奖励挂账处理先于闲聊过滤：
    # 开启全量消息后，邀请者在群里随便说一句话即可激活奖励到账
    awarded = resolve_message_identity(event)
    if awarded:
        await reply(
            event.to_ctx(),
            f"拉群奖励 +{awarded} 积分已到账喵~当前积分：{get_user_points(_account_key(event.limit_user_id))}",
        )
    if event.msg_id:
        await notify_pending_group_reward(event.group_openid, event.msg_id)

    if event.is_full_message and not event.is_at and not _is_group_command(event.to_ctx(), event.content):
        return

    if not _remember_group_message(event.group_openid, event.msg_id):
        _log.debug(
            f"[群消息去重] event={event.event_type} "
            f"group={event.group_openid} msg={event.msg_id}"
        )
        return

    _log.info(
        f"[群消息] event={event.event_type} group={event.group_openid} "
        f"user={event.user_openid[:8]}... images={len(event.image_urls)}: "
        f"{redact_preview(event.content)[:50]}"
    )
    await process_message(event.to_ctx(), event.content)


async def handle_c2c_message(data):
    event = adapt_message_event(C2C_MESSAGE_CREATE, data, FULL_MESSAGE_GROUP_IDS)
    if not event:
        return

    if not _remember_c2c_message(event.user_openid, event.msg_id):
        _log.debug(f"[私聊去重] user={event.user_openid[:8]}... msg={event.msg_id}")
        return

    _log.info(
        f"[私聊] user={event.user_openid[:8]}... images={len(event.image_urls)}: "
        f"{redact_preview(event.content)[:50]}"
    )
    resolve_message_identity(event)
    await process_message(event.to_ctx(), event.content)


async def handle_group_add_robot(data: dict) -> None:
    """被拉入新群：群人数 >=10 且当日奖励次数未用尽时，奖励邀请者积分（记到 union 账户）"""
    group_openid = str(data.get("group_openid", "") or "")
    operator = str(data.get("op_member_openid", "") or "")
    if not group_openid or not operator:
        _log.info(
            f"[拉群奖励] 事件缺少字段 group={group_openid[:8]}... op={operator[:8]}..."
        )
        return

    member_count = await fetch_group_member_count(group_openid)
    if member_count is None:
        # 查询失败时既不奖励也不提示，避免误判群人数
        return

    if member_count < GROUP_ADD_MIN_MEMBERS:
        _log.info(
            f"[拉群奖励] 群人数不足 group={group_openid[:8]}... members={member_count}"
        )
        await send_group_msg(
            group_openid,
            (
                f"感谢邀请喵~当前群人数 {member_count} 人，"
                f"达到 {GROUP_ADD_MIN_MEMBERS} 人后邀请奖励才会发放哦~"
            ),
            str(data.get("id", "") or ""),
        )
        return

    account = _account_key(operator)
    if account == operator:
        # 还不知道邀请者的统一身份：挂账，等他在本群发言后自动入账
        _queue_pending_reward(group_openid, operator)
        await send_group_msg(
            group_openid,
            (
                f"感谢把我拉进群喵~邀请者 @我 或发送「签到」"
                f"即可领取 +{GROUP_ADD_REWARD_POINTS} 积分~\n"
                f"（发送「积分」可查余额，「签到」每天 +{SIGN_IN_POINTS}）"
            ),
            str(data.get("id", "") or ""),
        )
        return

    reason, gained = award_group_add_points(account, group_openid)
    if reason == "ok":
        balance = get_user_points(account)
        _log.info(
            f"[拉群奖励] +{gained} group={group_openid[:8]}... "
            f"account={_account_label(account)} members={member_count} balance={balance}"
        )
        await send_group_msg(
            group_openid,
            (
                f"感谢把我拉进群喵~奖励邀请者 {gained} 积分！\n"
                f"当前积分：{balance}\n"
                f"（发送「积分」可查余额，「签到」每天 +{SIGN_IN_POINTS}）"
            ),
            str(data.get("id", "") or ""),
        )
    elif reason == "daily_limit":
        _log.info(f"[拉群奖励] 当日奖励次数已用尽 account={_account_label(account)}")
    elif reason == "duplicate_group":
        _log.info(
            f"[拉群奖励] 该群已奖励过 account={_account_label(account)} group={group_openid[:8]}..."
        )


# ====== Webhook ======
def _qq_webhook_key(app_secret: str):
    secret_bytes = str(app_secret or "").encode("utf-8")
    if not secret_bytes:
        raise ValueError("QQ_APP_SECRET is required for webhook validation")
    while len(secret_bytes) < 32:
        secret_bytes = (secret_bytes + secret_bytes)[:32]
    return ed25519.Ed25519PrivateKey.from_private_bytes(secret_bytes[:32])


def _build_qq_webhook_signature(app_secret: str, event_ts: str, plain_token: str) -> str:
    key = _qq_webhook_key(app_secret)
    return key.sign(f"{event_ts}{plain_token}".encode("utf-8")).hex()


def _verify_qq_webhook(body: bytes, headers) -> bool:
    if APP_ID and headers.get("X-Bot-Appid", "") != str(APP_ID):
        return False
    timestamp = headers.get("X-Signature-Timestamp", "")
    signature = headers.get("X-Signature-Ed25519", "")
    if not timestamp or not signature:
        return False
    try:
        _qq_webhook_key(APP_SECRET).public_key().verify(
            bytes.fromhex(signature), timestamp.encode("utf-8") + body
        )
    except (ValueError, InvalidSignature):
        return False
    return True


async def _receive_qq_webhook(request):
    from aiohttp import web

    body = await request.read()
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return web.json_response({"status": "error", "error": "invalid_json"}, status=400)
    if not isinstance(payload, dict):
        return web.json_response({"status": "error", "error": "invalid_payload"}, status=400)
    # QQ URL verification (op 13) uses the challenge/response handshake.
    # Dispatches must authenticate the exact bytes forwarded by the proxy.
    if APP_ID and request.headers.get("X-Bot-Appid", "") != str(APP_ID):
        _log.warning("[QQWebhook] rejected: invalid_appid")
        return web.json_response({"error": "invalid_appid"}, status=403)
    if payload.get("op") != 13 and not _verify_qq_webhook(body, request.headers):
        _log.warning("[QQWebhook] rejected: invalid_signature")
        return web.json_response({"error": "invalid_signature"}, status=403)
    if QQ_WEBHOOK_ASYNC and payload.get("op") == 0:
        # 事件先回包（QQ 只要 200），处理放到后台：AI、生图、发邮件再慢也不会让回调超时或 502。
        # 地址校验（op 13）仍同步应答，因为应答体就是校验结果。
        data = payload.get("d")
        _spawn_inbound(
            handle_qq_webhook_payload(payload),
            str(payload.get("t") or payload.get("event_type") or "?"),
            data.get("id", "") if isinstance(data, dict) else "",
        )
        return web.json_response({"status": "success"})
    try:
        result = await handle_qq_webhook_payload(payload)
    except Exception as error:
        _log.error(f"[QQWebhook] 处理失败: {error}")
        return web.json_response({"status": "error", "error": "handler_failed"}, status=500)
    return web.json_response(result)


# ====== 入口任务（webhook 和 WS 共用） ======
_inbound_tasks: set = set()


def _spawn_inbound(coro, event_type: str, event_id: str = "") -> asyncio.Task:
    """后台处理一个 QQ 事件：保留任务引用（防止被回收）、记录异常和处理耗时。"""
    task = asyncio.create_task(_run_inbound(coro, event_type, event_id))
    _inbound_tasks.add(task)
    task.add_done_callback(_inbound_tasks.discard)
    return task


async def _run_inbound(coro, event_type: str, event_id: str) -> None:
    started = _time_mod.monotonic()
    try:
        await coro
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.exception(f"[入口] {event_type} 处理失败 id={event_id[:24]}")
    finally:
        _log.info(f"[入口] {event_type} 处理完成 {(_time_mod.monotonic() - started) * 1000:.0f}ms")


async def drain_inbound_tasks(timeout: float | None = None) -> None:
    """等后台事件处理完（测试和退出时用）。"""
    pending = [task for task in list(_inbound_tasks) if not task.done()]
    if pending:
        await asyncio.wait(pending, timeout=timeout)


async def handle_qq_webhook_payload(payload: dict, app_secret: str | None = None):
    if not isinstance(payload, dict):
        return {"status": "ignored", "reason": "invalid_payload"}

    data = payload.get("d", {})
    if payload.get("op") == 13 and isinstance(data, dict) and "event_ts" in data and "plain_token" in data:
        plain_token = str(data.get("plain_token", ""))
        event_ts = str(data.get("event_ts", ""))
        # 平台回调地址校验要求 op-12 应答格式
        return {
            "op": 12,
            "d": {
                "random_key": plain_token,
                "evendata": _build_qq_webhook_signature(
                    app_secret or APP_SECRET,
                    event_ts,
                    plain_token,
                ),
            },
        }

    if payload.get("op") != 0:
        return {"status": "ignored", "reason": "unsupported_opcode"}
    event_type = str(payload.get("t") or payload.get("event_type") or "")
    if not isinstance(data, dict):
        data = {}
    await dispatch_qq_event(event_type, data, source="webhook")
    return {"status": "success"}


async def dispatch_qq_event(event_type: str, data: dict, source: str = "webhook") -> None:
    """webhook 和 WS 收到的事件走同一套分发。"""
    if not isinstance(data, dict):
        data = {}
    if event_type in GROUP_MESSAGE_EVENTS:
        if event_type != GROUP_AT_MESSAGE_CREATE:
            _log.info(
                "[QQWebhook] dispatch=%s group_allowed=%s source=%s",
                event_type,
                not FULL_MESSAGE_GROUP_IDS or data.get("group_openid", "") in FULL_MESSAGE_GROUP_IDS,
                source,
            )
        await handle_group_message(data, event_type)
    elif event_type == C2C_MESSAGE_CREATE:
        await handle_c2c_message(data)
    elif event_type == "GROUP_ADD_ROBOT":
        _log.info(f"[事件] GROUP_ADD_ROBOT source={source}")
        await handle_group_add_robot(data)
    elif event_type in ("GROUP_DEL_ROBOT", "FRIEND_ADD", "FRIEND_DEL"):
        _log.info(f"[事件] {event_type} source={source}")
    elif event_type:
        _log.info(
            f"[事件未处理] t={event_type} source={source} keys={list(data.keys())} "
            f"group={data.get('group_openid', '')} id={data.get('id', '')}"
        )
    else:
        _log.debug("[QQWebhook] ignored payload without event type")


async def run_webhook_server():
    """QQ 回调监听（127.0.0.1:8765，由 bridge.enabled / QQ_BRIDGE_ENABLED 打开）。

    名字里以前叫 KoishiBridge，其实是生产的主入口：控制台 9000 端口的 /qq 转发到这里。
    """
    from aiohttp import web

    async def health(_request):
        return web.json_response({"ok": True})

    app = web.Application()
    app.router.add_get("/health", health)
    app.router.add_post(QQ_WEBHOOK_PATH, _receive_qq_webhook)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, BRIDGE_HOST, BRIDGE_PORT)
    await site.start()
    _log.info(
        f"[Webhook] listening on http://{BRIDGE_HOST}:{BRIDGE_PORT}, "
        f"qq_webhook={QQ_WEBHOOK_PATH}, async_ack={QQ_WEBHOOK_ASYNC}"
    )

    try:
        while True:
            await asyncio.sleep(3600)
    finally:
        await runner.cleanup()


# 旧名字，测试和外部脚本还在用
run_koishi_bridge_server = run_webhook_server


# ====== WebSocket ======
async def _send_heartbeat_once(ws, seq):
    try:
        await ws.send(json.dumps({"op": 1, "d": seq}))
        return True
    except Exception as e:
        _log.warning(f"[WS] 心跳发送失败，准备重连: {e}")
        try:
            await ws.close()
        except Exception as close_error:
            _log.debug(f"[WS] 心跳失败后关闭连接也失败: {close_error}")
        return False


async def run_websocket():
    reconnect_delay = 5
    session_id = ""
    last_seq = None

    while True:
        try:
            await refresh_access_token()
            gateway_url = await get_gateway_url()
            if not gateway_url:
                _log.error("[Gateway] 失败，5s 后重试")
                await asyncio.sleep(5)
                continue

            async with websockets.connect(
                gateway_url, max_size=10 * 1024 * 1024, ping_interval=None
            ) as ws:
                _log.info("[WS] 已连接")
                heartbeat_interval = 0
                heartbeat_task = None

                async def send_heartbeat():
                    while True:
                        await asyncio.sleep(heartbeat_interval / 1000)
                        if not await _send_heartbeat_once(ws, last_seq):
                            return

                try:
                    async for raw in ws:
                        msg = json.loads(raw)
                        op, t, d, s = (
                            msg.get("op"),
                            msg.get("t"),
                            msg.get("d", {}),
                            msg.get("s"),
                        )
                        if s is not None:
                            last_seq = s

                        if op == 10:
                            heartbeat_interval = d.get("heartbeat_interval", 41250)
                            _log.info(f"[WS] Hello, hb={heartbeat_interval}ms")
                            if session_id and last_seq is not None:
                                await ws.send(
                                    json.dumps(
                                        {
                                            "op": 6,
                                            "d": {
                                                "token": f"QQBot {qq_api._access_token}",
                                                "session_id": session_id,
                                                "seq": last_seq,
                                            },
                                        }
                                    )
                                )
                            else:
                                await ws.send(
                                    json.dumps(
                                        {
                                            "op": 2,
                                            "d": {
                                                "token": f"QQBot {qq_api._access_token}",
                                                "intents": GATEWAY_INTENTS,
                                                "shard": [0, 1],
                                            },
                                        }
                                    )
                                )
                            heartbeat_task = asyncio.create_task(send_heartbeat())
                            reconnect_delay = 5

                        elif op == 0:
                            if t == "READY":
                                session_id = d.get("session_id", "")
                                _log.info(
                                    f"[WS] Ready! {d.get('user', {}).get('username', '?')}"
                                )
                            elif t == "RESUMED":
                                _log.info("[WS] Resumed")
                            elif t:
                                # 和 webhook 同一套分发；任务有引用、有异常日志、有耗时
                                _spawn_inbound(
                                    dispatch_qq_event(t, d, source="ws"),
                                    t,
                                    d.get("id", "") if isinstance(d, dict) else "",
                                )
                            else:
                                _log.debug(f"[WS] {t}")

                        elif op == 11:
                            pass
                        elif op == 7:
                            _log.warning("[WS] 要求重连")
                            break
                        elif op == 9:
                            _log.warning(f"[WS] Session 失效/鉴权失败: d={d}")
                            session_id = ""
                            last_seq = None
                            break
                finally:
                    if heartbeat_task:
                        heartbeat_task.cancel()

        except websockets.exceptions.ConnectionClosed as e:
            _log.warning(f"[WS] 断开: {e}")
        except Exception as e:
            _log.error(f"[WS] 异常: {e}")

        _log.info(f"[WS] {reconnect_delay}s 后重连")
        await asyncio.sleep(reconnect_delay)
        reconnect_delay = min(reconnect_delay * 1.5, 60)
