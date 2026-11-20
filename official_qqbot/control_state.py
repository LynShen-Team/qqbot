"""控制台状态：管理员/Staff/封禁/邮箱绑定（云模式走控制台 API，本地模式走 data/*.json）和日志上报。"""

import os

import json_state
from redact import redact_outbound, redact_preview
from config import BOT_CONTROL_TOKEN, CONTROL_API_BASE_URL, _BOT_DIR, _log
from state_backend import LocalStateBackend, build_state_backend

# ====== Admin/Staff/Ban 系统（openid） ======
ADMIN_FILE = os.path.join(_BOT_DIR, "data", "admins.json")
STAFF_FILE = os.path.join(_BOT_DIR, "data", "staff.json")
BAN_FILE = os.path.join(_BOT_DIR, "data", "banned.json")


def _load_json_set(filepath) -> set[str]:
    data = json_state.read_json(filepath, [])
    try:
        return set(str(x) for x in data)
    except TypeError:
        return set()


def _save_json_set(filepath, data: set):
    json_state.write_json(filepath, list(data))


def _load_staff_dict() -> dict[str, dict]:
    data = json_state.read_json(STAFF_FILE, {})
    return data if isinstance(data, dict) else {}


def _save_staff_dict():
    json_state.write_json(STAFF_FILE, _staff, indent=2)


_admin_set: set[str] = _load_json_set(ADMIN_FILE)
_staff: dict[str, dict] = _load_staff_dict()
_staff_logged_in: set[str] = set()
_banned_set: set[str] = _load_json_set(BAN_FILE)

_log.info(
    f"[Admin] 已加载 {len(_admin_set)} 个管理员, {len(_staff)} 个 Staff, {len(_banned_set)} 个封禁"
)


def _is_admin(user_openid: str) -> bool:
    return _state_backend.is_admin(user_openid)


def _is_admin_or_staff(user_openid: str) -> bool:
    return _state_backend.is_admin_or_staff(user_openid)


def _is_banned(user_openid: str) -> bool:
    return _state_backend.is_banned(user_openid)


# ====== 邮箱绑定 {openid: email} ======
EMAIL_BIND_FILE = os.path.join(_BOT_DIR, "data", "email_binds.json")


def _load_email_binds() -> dict[str, str]:
    data = json_state.read_json(EMAIL_BIND_FILE, {})
    return data if isinstance(data, dict) else {}


def _save_email_binds():
    json_state.write_json(EMAIL_BIND_FILE, _email_binds, indent=2)


_email_binds: dict[str, str] = _load_email_binds()
_log.info(f"[邮箱] 已加载 {len(_email_binds)} 个邮箱绑定")

_state_backend = build_state_backend(CONTROL_API_BASE_URL, BOT_CONTROL_TOKEN)
if isinstance(_state_backend, LocalStateBackend):
    _state_backend.bind_memory(
        admin_set=_admin_set,
        staff=_staff,
        staff_logged_in=_staff_logged_in,
        banned_set=_banned_set,
        email_binds=_email_binds,
        save_admins=lambda: _save_json_set(ADMIN_FILE, _admin_set),
        save_staff=_save_staff_dict,
        save_banned=lambda: _save_json_set(BAN_FILE, _banned_set),
        save_email_binds=_save_email_binds,
    )
_log.info(f"[State] backend={_state_backend.backend_name}")


def _first_token(content: str) -> str:
    parts = str(content or "").strip().split()
    return parts[0].lower() if parts else ""


def _record_seen_user(ctx: dict) -> None:
    try:
        _state_backend.seen_user(
            ctx.get("user_openid", ""),
            group_openid=ctx.get("group_openid", ""),
        )
    except AttributeError:
        return
    except Exception as e:
        _log.warning(f"[ControlLog] seen_user failed: {e}")


def _log_command_event(ctx: dict, content: str) -> None:
    try:
        _state_backend.log_command(
            user_key=ctx.get("user_openid", ""),
            group_openid=ctx.get("group_openid", ""),
            command=_first_token(content),
            content=redact_preview(content),  # /auth 口令等不进命令日志
            message_id=ctx.get("msg_id", ""),
        )
    except AttributeError:
        return
    except Exception as e:
        _log.warning(f"[ControlLog] command failed: {e}")


def _log_outbound_event(
    ctx: dict,
    channel: str,
    ok: bool,
    content: str,
    message_id: str | None = None,
) -> None:
    try:
        _state_backend.log_outbound(
            user_key=ctx.get("user_openid", ""),
            group_openid=ctx.get("group_openid", ""),
            channel=channel,
            status="success" if ok else "failed",
            content=redact_outbound(content),  # 发号的账号密码不进发送日志
            message_id=message_id or ctx.get("msg_id", ""),
        )
    except AttributeError:
        return
    except Exception as e:
        _log.warning(f"[ControlLog] outbound failed: {e}")


def _log_email_outbound(
    ctx: dict,
    user_id: str,
    ok: bool,
    subject: str,
    masked_addr: str,
) -> None:
    try:
        _state_backend.log_outbound(
            user_key=user_id,
            group_openid=ctx.get("group_openid", ""),
            channel="email",
            status="success" if ok else "failed",
            content=f"{subject} -> {masked_addr}",
            message_id=ctx.get("msg_id", ""),
        )
    except AttributeError:
        return
    except Exception as e:
        _log.warning(f"[ControlLog] email failed: {e}")
