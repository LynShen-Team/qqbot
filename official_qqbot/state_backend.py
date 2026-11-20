from __future__ import annotations

import asyncio
import contextvars
import logging
from pathlib import Path
from typing import Callable

import json_state
import shared_cooldown
from control_api.client import AsyncControlApiClient, ControlApiClient

_log = logging.getLogger("QQBot")

# 每条消息处理期间缓存一次用户状态（封禁/邮箱/权限），同一条消息里不再重复问控制台。
# 每条消息在自己的 asyncio 任务里处理，ContextVar 天然按消息隔离。
_user_state_cache: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "qqbot_user_state_cache", default=None
)


class ControlEventQueue:
    """Ships fire-and-forget control calls (command/outbound logs, "seen") off the hot path.

    Messages used to wait for 2-4 synchronous HTTP calls to the control API just to write
    logs. They are now queued and sent by one background task per event loop. The queue is
    bounded; when it is full, new events are dropped with a (rate-limited) warning instead
    of slowing the bot down.
    """

    def __init__(self, client: AsyncControlApiClient, maxsize: int = 5000):
        self.client = client
        self.maxsize = maxsize
        self._queue: asyncio.Queue | None = None
        self._worker: asyncio.Task | None = None
        self._loop = None
        self.dropped = 0
        self.failed = 0

    def submit(self, method: str, **fields) -> bool:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return False
        if self._loop is not loop or self._queue is None:
            self._loop = loop
            self._queue = asyncio.Queue(maxsize=self.maxsize)
            self._worker = None
        if self._worker is None or self._worker.done():
            self._worker = loop.create_task(self._run(), name="control-event-queue")
        try:
            self._queue.put_nowait((method, fields))
            return True
        except asyncio.QueueFull:
            self.dropped += 1
            if self.dropped in (1, 10, 100) or self.dropped % 1000 == 0:
                _log.warning("[ControlQueue] 队列已满，丢弃日志事件（累计 %s 条）", self.dropped)
            return True

    async def _run(self) -> None:
        queue = self._queue
        while True:
            method, fields = await queue.get()
            try:
                await getattr(self.client, method)(**fields)
            except Exception as error:  # noqa: BLE001 - logged, keeps draining
                self.failed += 1
                _log.warning("[ControlQueue] %s 失败: %s", method, error)
            finally:
                queue.task_done()

    async def drain(self, timeout: float = 5.0) -> None:
        if self._queue is None:
            return
        try:
            await asyncio.wait_for(self._queue.join(), timeout)
        except asyncio.TimeoutError:
            _log.warning("[ControlQueue] 退出时还有 %s 条日志没发完", self._queue.qsize())


DATA_DIR = Path(__file__).resolve().parent / "data"


class LocalStateBackend:
    backend_name = "local"

    def __init__(self, data_dir: str | Path | None = None):
        self.data_dir = Path(data_dir) if data_dir else DATA_DIR
        self._admin_set: set[str] | None = None
        self._staff: dict[str, dict] | None = None
        self._staff_logged_in: set[str] | None = None
        self._banned_set: set[str] | None = None
        self._email_binds: dict[str, str] | None = None
        self._save_admins: Callable[[], None] | None = None
        self._save_staff: Callable[[], None] | None = None
        self._save_banned: Callable[[], None] | None = None
        self._save_email_binds: Callable[[], None] | None = None

    def bind_memory(
        self,
        admin_set: set[str],
        staff: dict[str, dict],
        staff_logged_in: set[str],
        banned_set: set[str],
        email_binds: dict[str, str],
        save_admins: Callable[[], None],
        save_staff: Callable[[], None],
        save_banned: Callable[[], None],
        save_email_binds: Callable[[], None],
    ) -> None:
        self._admin_set = admin_set
        self._staff = staff
        self._staff_logged_in = staff_logged_in
        self._banned_set = banned_set
        self._email_binds = email_binds
        self._save_admins = save_admins
        self._save_staff = save_staff
        self._save_banned = save_banned
        self._save_email_binds = save_email_binds

    def is_admin(self, user_key: str) -> bool:
        return user_key in self._admins()

    def is_admin_or_staff(self, user_key: str) -> bool:
        return self.is_admin(user_key) or user_key in self._staff_online()

    def is_banned(self, user_key: str) -> bool:
        return user_key in self._banned()

    def get_email_binding(self, user_key: str) -> str:
        return self._emails().get(user_key, "").strip()

    def set_email_binding(self, user_key: str, email: str) -> None:
        self._emails()[user_key] = email
        self._save_emails()

    def delete_email_binding(self, user_key: str) -> bool:
        if user_key not in self._emails():
            return False
        del self._emails()[user_key]
        self._save_emails()
        return True

    def add_admin(self, user_key: str) -> None:
        self._admins().add(user_key)
        self._save_admin_set()

    def remove_admin(self, user_key: str) -> bool:
        if user_key not in self._admins():
            return False
        self._admins().discard(user_key)
        self._save_admin_set()
        return True

    def staff_exists(self, user_key: str) -> bool:
        return user_key in self._staff_map()

    def get_staff_password(self, user_key: str) -> str:
        info = self._staff_map().get(user_key) or {}
        return str(info.get("password", ""))

    def set_staff_password(self, user_key: str, password: str) -> None:
        staff = self._staff_map()
        info = staff.setdefault(user_key, {})
        info["password"] = password
        self._save_staff_map()

    def login_staff(self, user_key: str) -> None:
        self._staff_online().add(user_key)

    def logout(self, user_key: str) -> bool:
        removed = self.remove_admin(user_key)
        if user_key in self._staff_online():
            self._staff_online().discard(user_key)
            removed = True
        return removed

    def add_staff(self, user_key: str, added_by: str = "") -> bool:
        if user_key in self._staff_map():
            return False
        self._staff_map()[user_key] = {"password": "", "added_by": added_by}
        self._save_staff_map()
        return True

    def delete_staff(self, user_key: str) -> bool:
        if user_key not in self._staff_map():
            return False
        del self._staff_map()[user_key]
        self._staff_online().discard(user_key)
        self._save_staff_map()
        return True

    def ban_user(self, user_key: str) -> None:
        self._banned().add(user_key)
        self._save_banned_set()

    def unban_user(self, user_key: str) -> bool:
        if user_key not in self._banned():
            return False
        self._banned().discard(user_key)
        self._save_banned_set()
        return True

    def counts(self) -> dict:
        return {
            "admins": len(self._admins()),
            "staff": len(self._staff_map()),
            "staff_online": len(self._staff_online()),
            "banned": len(self._banned()),
        }

    def set_resource_limit(self, feature: str, limit: int, unit: str, updated_by: str = "") -> None:
        shared_cooldown.set_restrict_rule(feature, limit, unit)

    def check_resource_limit(self, feature: str, user_key: str) -> tuple[bool, int, int, int]:
        return shared_cooldown.check_restrict_limit(feature, user_key)

    def get_resource_limit_status(self, feature: str, user_key: str) -> dict:
        return shared_cooldown.get_restrict_status(feature, user_key)

    def record_resource_usage(self, feature: str, user_key: str, ctx: dict | None = None) -> None:
        shared_cooldown.record_restrict_usage(feature, user_key)

    def reset_resource_usage(self) -> None:
        shared_cooldown.reset_restrict_usage()

    def list_resource_usage_stats(self) -> list[dict]:
        return shared_cooldown.get_restrict_usage_stats()

    async def prefetch_user_state(self, user_key: str):
        return None

    @staticmethod
    def end_message(token) -> None:
        return None

    async def drain(self, timeout: float = 5.0) -> None:
        return None

    def seen_user(self, user_key: str, display_name: str = "", group_openid: str = "") -> None:
        return None

    def log_command(
        self,
        user_key: str = "",
        group_openid: str = "",
        command: str = "",
        content: str = "",
        message_id: str = "",
    ) -> None:
        return None

    def log_outbound(
        self,
        user_key: str = "",
        group_openid: str = "",
        channel: str = "",
        status: str = "",
        content: str = "",
        message_id: str = "",
    ) -> None:
        return None

    def _admins(self) -> set[str]:
        if self._admin_set is not None:
            return self._admin_set
        return set(_read_json(self.data_dir / "admins.json", []))

    def _staff_map(self) -> dict[str, dict]:
        if self._staff is not None:
            return self._staff
        data = _read_json(self.data_dir / "staff.json", {})
        return data if isinstance(data, dict) else {}

    def _staff_online(self) -> set[str]:
        if self._staff_logged_in is not None:
            return self._staff_logged_in
        return set()

    def _banned(self) -> set[str]:
        if self._banned_set is not None:
            return self._banned_set
        return set(_read_json(self.data_dir / "banned.json", []))

    def _emails(self) -> dict[str, str]:
        if self._email_binds is not None:
            return self._email_binds
        data = _read_json(self.data_dir / "email_binds.json", {})
        return data if isinstance(data, dict) else {}

    def _save_admin_set(self) -> None:
        if self._save_admins:
            self._save_admins()
            return
        _write_json(self.data_dir / "admins.json", sorted(self._admins()))

    def _save_staff_map(self) -> None:
        if self._save_staff:
            self._save_staff()
            return
        _write_json(self.data_dir / "staff.json", self._staff_map())

    def _save_banned_set(self) -> None:
        if self._save_banned:
            self._save_banned()
            return
        _write_json(self.data_dir / "banned.json", sorted(self._banned()))

    def _save_emails(self) -> None:
        if self._save_email_binds:
            self._save_email_binds()
            return
        _write_json(self.data_dir / "email_binds.json", self._emails())


class CloudStateBackend:
    backend_name = "cloud"

    def __init__(
        self,
        client: ControlApiClient,
        async_client: AsyncControlApiClient | None = None,
    ):
        self.client = client
        self.async_client = async_client
        self.events = ControlEventQueue(async_client) if async_client else None
        self._staff_logged_in: set[str] = set()

    # ---- per-message user state ----
    async def prefetch_user_state(self, user_key: str):
        """Fetch the sender's state once, without blocking the loop, for the current message.

        Returns a token for end_message(); state read during the message comes from the cache.
        """
        cache: dict = {}
        token = _user_state_cache.set(cache)
        if user_key and self.async_client is not None:
            try:
                cache[user_key] = await self.async_client.get_user_state(user_key)
            except Exception as error:  # noqa: BLE001 - falls back to the synchronous fetch
                _log.warning("[ControlLog] prefetch user state failed: %s", error)
        return token

    @staticmethod
    def end_message(token) -> None:
        if token is not None:
            try:
                _user_state_cache.reset(token)
            except ValueError:
                _user_state_cache.set(None)

    def _state(self, user_key: str) -> dict:
        cache = _user_state_cache.get()
        if cache is not None and user_key in cache:
            return cache[user_key]
        state = self.client.get_user_state(user_key)
        if cache is not None:
            cache[user_key] = state
        return state

    @staticmethod
    def _forget(user_key: str) -> None:
        cache = _user_state_cache.get()
        if cache is not None:
            cache.pop(user_key, None)

    def is_admin(self, user_key: str) -> bool:
        return bool(self._state(user_key).get("is_admin"))

    def is_admin_or_staff(self, user_key: str) -> bool:
        state = self._state(user_key)
        return bool(state.get("is_admin")) or user_key in self._staff_logged_in

    def is_banned(self, user_key: str) -> bool:
        return bool(self._state(user_key).get("is_banned"))

    def get_email_binding(self, user_key: str) -> str:
        return str(self._state(user_key).get("email") or "").strip()

    def set_email_binding(self, user_key: str, email: str) -> None:
        self._forget(user_key)
        self.client.set_email_binding(user_key, email)

    def delete_email_binding(self, user_key: str) -> bool:
        self._forget(user_key)
        self.client.delete_email_binding(user_key)
        return True

    def add_admin(self, user_key: str) -> None:
        self._forget(user_key)
        self.client.add_role(user_key, "admin")

    def remove_admin(self, user_key: str) -> bool:
        self._forget(user_key)
        self.client.delete_role(user_key, "admin")
        return True

    def staff_exists(self, user_key: str) -> bool:
        return "staff" in self._state(user_key).get("roles", [])

    def get_staff_password(self, user_key: str) -> str:
        return str(self.client.get_role(user_key, "staff").get("password") or "")

    def set_staff_password(self, user_key: str, password: str) -> None:
        self._forget(user_key)
        self.client.add_role(user_key, "staff", password=password)

    def login_staff(self, user_key: str) -> None:
        self._staff_logged_in.add(user_key)

    def logout(self, user_key: str) -> bool:
        self.remove_admin(user_key)
        if user_key in self._staff_logged_in:
            self._staff_logged_in.discard(user_key)
            return True
        return True

    def add_staff(self, user_key: str, added_by: str = "") -> bool:
        self._forget(user_key)
        self.client.add_role(user_key, "staff", added_by=added_by)
        return True

    def delete_staff(self, user_key: str) -> bool:
        self._forget(user_key)
        self.client.delete_role(user_key, "staff")
        self._staff_logged_in.discard(user_key)
        return True

    def ban_user(self, user_key: str) -> None:
        self._forget(user_key)
        self.client.set_banned(user_key, True)

    def unban_user(self, user_key: str) -> bool:
        self._forget(user_key)
        self.client.set_banned(user_key, False)
        return True

    def counts(self) -> dict:
        data = self.client.counts()
        return {
            "admins": int(data.get("admins", 0) or 0),
            "staff": int(data.get("staff", 0) or 0),
            "staff_online": len(self._staff_logged_in),
            "banned": int(data.get("banned", 0) or 0),
        }

    def set_resource_limit(self, feature: str, limit: int, unit: str, updated_by: str = "") -> None:
        self.client.set_resource_limit(feature, limit, unit, updated_by=updated_by)

    def check_resource_limit(self, feature: str, user_key: str) -> tuple[bool, int, int, int]:
        result = self.client.check_resource_limit(feature, user_key)
        return (
            bool(result.get("blocked")),
            int(result.get("count", 0)),
            int(result.get("limit", 0)),
            int(result.get("window", 0)),
        )

    def get_resource_limit_status(self, feature: str, user_key: str) -> dict:
        result = self.client.check_resource_limit(feature, user_key)
        return {
            "blocked": bool(result.get("blocked")),
            "count": int(result.get("count", 0)),
            "limit": int(result.get("limit", 0)),
            "window": int(result.get("window", 0)),
            "reset_after": int(result.get("reset_after", 0)),
        }

    def record_resource_usage(self, feature: str, user_key: str, ctx: dict | None = None) -> None:
        ctx = ctx or {}
        self.client.record_resource_usage(
            feature,
            user_key,
            source_group_openid=ctx.get("group_openid", ""),
            message_id=ctx.get("msg_id", ""),
        )

    def reset_resource_usage(self) -> None:
        self.client.reset_resource_usage()

    def list_resource_usage_stats(self) -> list[dict]:
        return list(self.client.list_resource_usage_stats().get("items", []))

    # ---- fire-and-forget events: queued, sent in the background ----
    def _submit(self, method: str, **fields) -> None:
        if self.events is not None and self.events.submit(method, **fields):
            return
        getattr(self.client, method)(**fields)

    def seen_user(self, user_key: str, display_name: str = "", group_openid: str = "") -> None:
        self._submit("seen_user", user_key=user_key, display_name=display_name, group_openid=group_openid)

    def log_command(
        self,
        user_key: str = "",
        group_openid: str = "",
        command: str = "",
        content: str = "",
        message_id: str = "",
    ) -> None:
        self._submit(
            "log_command",
            user_key=user_key,
            group_openid=group_openid,
            command=command,
            content=content,
            message_id=message_id,
        )

    def log_outbound(
        self,
        user_key: str = "",
        group_openid: str = "",
        channel: str = "",
        status: str = "",
        content: str = "",
        message_id: str = "",
    ) -> None:
        self._submit(
            "log_outbound",
            user_key=user_key,
            group_openid=group_openid,
            channel=channel,
            status=status,
            content=content,
            message_id=message_id,
        )

    async def drain(self, timeout: float = 5.0) -> None:
        if self.events is not None:
            await self.events.drain(timeout)


def build_state_backend(control_api_base_url: str, bot_token: str):
    if control_api_base_url:
        return CloudStateBackend(
            ControlApiClient(base_url=control_api_base_url, bot_token=bot_token),
            AsyncControlApiClient(base_url=control_api_base_url, bot_token=bot_token),
        )
    return LocalStateBackend()


def _read_json(path: Path, default):
    return json_state.read_json(path, default)


def _write_json(path: Path, value) -> None:
    json_state.write_json(path, value, indent=2)
