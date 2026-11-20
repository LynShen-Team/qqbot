"""
共享冷却/封禁/频控状态（NFA、163、sauth、4399 的冷却、频率、封禁、/restrict 规则）

两种存储，逻辑相同：
- JSON（默认，BOT_STATE_STORE=json）：data/cooldowns.json，内存缓存 + 每秒最多重读一次，
  写入走 json_state（临时文件 + fsync + 替换）。
- 数据库（use_database(engine)）：cooldown_entries 表，每个用户一行，事务 + 行锁，
  不再每次把整个 500KB 的文件重写一遍。
"""

import json
import logging
import os
import time
from contextlib import contextmanager

import json_state

_log = logging.getLogger("QQBot")

SHARED_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "data", "cooldowns.json"
)

# 内存缓存 + 文件同步（JSON 存储）
_cache: dict = {}
_last_load_ts: float = 0
RESTRICT_WINDOWS = {
    "min": 60,
    "hour": 3600,
    "day": 86400,
    "month": 30 * 86400,
    "quarter": 90 * 86400,
    "year": 365 * 86400,
}
RESTRICT_FEATURES = {"163", "4399", "nfa"}
RESTRICT_STATS_WINDOWS = {
    "second": 1,
    "minute": 60,
    "hour": 3600,
    "day": 86400,
    "month": 30 * 86400,
    "year": 365 * 86400,
}
_RELOAD_INTERVAL = 1.0  # 每次检查前最多 1 秒读一次文件

_db_store = None  # state_db.SqlCooldownStore when BOT_STATE_STORE=db


def use_database(store) -> None:
    """切到数据库存储（None = 回到 JSON 文件）。"""
    global _db_store
    _db_store = store


def _load():
    """从共享文件加载状态"""
    global _cache, _last_load_ts
    now = time.time()
    if now - _last_load_ts < _RELOAD_INTERVAL:
        return
    _last_load_ts = now
    if not os.path.exists(SHARED_FILE):
        _cache = {}
        return
    data = json_state.read_json(SHARED_FILE, None, label="cooldowns.json")
    if isinstance(data, dict):
        _cache = data
    # 读坏了保留内存里的上一份；json_state 已报警并禁止覆盖这个文件


def _save():
    """把状态写回共享文件（临时文件 + fsync + 替换）"""
    json_state.write_json(SHARED_FILE, _cache, label="cooldowns.json")


def _section(feature: str) -> dict:
    """获取某个功能的子 dict（JSON 存储）"""
    if feature not in _cache:
        _cache[feature] = {}
    return _cache[feature]


class _JsonTx:
    """JSON 存储的事务：读写内存缓存，有改动时整个文件保存一次。

    "restrict_log:<feature>" 对应文件里的嵌套结构 restrict_log -> feature -> uid。
    """

    def __init__(self):
        self.dirty = False

    @staticmethod
    def _dict(section: str) -> dict:
        if section.startswith("restrict_log:"):
            return _section("restrict_log").setdefault(section.split(":", 1)[1], {})
        return _section(section)

    def get(self, section, key):
        return self._dict(section).get(str(key))

    def put(self, section, key, value):
        self._dict(section)[str(key)] = value
        self.dirty = True

    def delete(self, section, key):
        if self._dict(section).pop(str(key), None) is not None:
            self.dirty = True

    def items(self, section):
        return dict(self._dict(section))

    def clear_prefix(self, prefix):
        if prefix == "restrict_log:":
            _cache["restrict_log"] = {}
            self.dirty = True


@contextmanager
def _tx():
    if _db_store is not None:
        with _db_store.transaction() as tx:
            yield tx
        return
    _load()
    tx = _JsonTx()
    yield tx
    if tx.dirty:
        _save()


# ========== 通用接口 ==========


def is_banned(feature: str, user_id: int) -> tuple[bool, int, int]:
    """
    检查用户是否被封禁
    返回 (是否封禁, 剩余小时, 剩余分钟)
    """
    with _tx() as tx:
        ban_until = tx.get(f"{feature}_ban", user_id) or 0
        now = time.time()
        if ban_until > now:
            remain = ban_until - now
            return True, int(remain / 3600), int(remain % 3600 / 60)
        if ban_until:
            tx.delete(f"{feature}_ban", user_id)
    return False, 0, 0


def check_cooldown(
    feature: str, user_id: int, cooldown_seconds: int
) -> tuple[bool, int]:
    """
    检查冷却是否已过
    返回 (是否在冷却中, 剩余秒数)
    """
    with _tx() as tx:
        last = tx.get(f"{feature}_cd", user_id) or 0
    elapsed = time.time() - last
    if elapsed < cooldown_seconds:
        return True, int(cooldown_seconds - elapsed)
    return False, 0


def check_rate_limit(
    feature: str, user_id, limit: int, window: int
) -> tuple[bool, int, int]:
    """
    滑动窗口频控（只拦截，不封禁）
    返回 (是否超限, 窗口内已用次数, 距解限秒数)
    """
    now = time.time()
    with _tx() as tx:
        recent = [float(ts) for ts in (tx.get(f"{feature}_rate", user_id) or []) if now - float(ts) < window]
        tx.put(f"{feature}_rate", user_id, recent)
    if len(recent) >= limit:
        retry_after = max(0, int(window - (now - min(recent))))
        return True, len(recent), retry_after
    return False, len(recent), 0


def record_rate_usage(feature: str, user_id) -> None:
    """记录一次成功获取，供滑动窗口频控与用量统计使用"""
    now = time.time()
    with _tx() as tx:
        recent = [float(ts) for ts in (tx.get(f"{feature}_rate", user_id) or []) if now - float(ts) < 3600]
        recent.append(now)
        tx.put(f"{feature}_rate", user_id, recent)


def check_hour_limit(
    feature: str, user_id: int, limit: int, ban_duration: int
) -> tuple[bool, int]:
    """
    检查一小时内是否超限，超限则自动封禁
    返回 (是否超限, 本小时已获取次数)
    """
    now = time.time()
    with _tx() as tx:
        hour_log = [ts for ts in (tx.get(f"{feature}_log", user_id) or []) if now - ts < 3600]
        if len(hour_log) >= limit:
            # 触发封禁
            tx.put(f"{feature}_ban", user_id, now + ban_duration)
            tx.delete(f"{feature}_log", user_id)
            return True, len(hour_log)
    return False, len(hour_log)


def record_usage(feature: str, user_id: int):
    """
    记录一次成功获取：写入冷却 + 频率记录
    必须在 check 通过之后、调 API 之前调用（防并发）
    """
    now = time.time()
    with _tx() as tx:
        tx.put(f"{feature}_cd", user_id, now)
        hour_log = [ts for ts in (tx.get(f"{feature}_log", user_id) or []) if now - ts < 3600]
        hour_log.append(now)
        tx.put(f"{feature}_log", user_id, hour_log)


def set_restrict_rule(feature: str, limit: int, unit: str) -> None:
    feature = str(feature).strip().lower()
    unit = str(unit).strip().lower()
    if feature not in RESTRICT_FEATURES:
        raise ValueError("invalid feature")
    if limit <= 0:
        raise ValueError("invalid limit")
    if unit not in RESTRICT_WINDOWS:
        raise ValueError("invalid unit")

    with _tx() as tx:
        tx.put("restrict_rules", feature, {
            "limit": int(limit),
            "unit": unit,
            "window": RESTRICT_WINDOWS[unit],
        })


def get_restrict_rule(feature: str) -> dict | None:
    with _tx() as tx:
        rule = tx.get("restrict_rules", str(feature).strip().lower())
    return rule if isinstance(rule, dict) else None


def get_restrict_status(feature: str, user_id) -> dict:
    feature = str(feature).strip().lower()
    rule = get_restrict_rule(feature)
    if not rule:
        return {
            "blocked": False,
            "count": 0,
            "limit": 0,
            "window": 0,
            "reset_after": 0,
        }

    limit = int(rule.get("limit", 0) or 0)
    window = int(rule.get("window", 0) or 0)
    if limit <= 0 or window <= 0:
        return {
            "blocked": False,
            "count": 0,
            "limit": 0,
            "window": 0,
            "reset_after": 0,
        }

    now = time.time()
    with _tx() as tx:
        recent = [
            float(ts)
            for ts in (tx.get(f"restrict_log:{feature}", user_id) or [])
            if now - float(ts) < window
        ]
        tx.put(f"restrict_log:{feature}", user_id, recent)

    reset_after = 0
    if recent:
        reset_after = max(0, int(window - (now - min(recent))))

    return {
        "blocked": len(recent) >= limit,
        "count": len(recent),
        "limit": limit,
        "window": window,
        "reset_after": reset_after,
    }


def check_restrict_limit(feature: str, user_id) -> tuple[bool, int, int, int]:
    status = get_restrict_status(feature, user_id)
    return (
        bool(status["blocked"]),
        int(status["count"]),
        int(status["limit"]),
        int(status["window"]),
    )


def record_restrict_usage(feature: str, user_id) -> None:
    feature = str(feature).strip().lower()
    if not get_restrict_rule(feature):
        return

    now = time.time()
    with _tx() as tx:
        recent = list(tx.get(f"restrict_log:{feature}", user_id) or [])
        recent.append(now)
        tx.put(f"restrict_log:{feature}", user_id, recent)


def reset_restrict_usage() -> None:
    """清空所有资源获取限制记录，保留 /restrict 规则。"""
    with _tx() as tx:
        tx.clear_prefix("restrict_log:")


def get_restrict_usage_stats() -> list[dict]:
    now = time.time()
    items = []
    with _tx() as tx:
        rules = tx.items("restrict_rules")
        for feature in sorted(RESTRICT_FEATURES):
            rule = rules.get(feature)
            if not isinstance(rule, dict):
                continue
            feature_logs = tx.items(f"restrict_log:{feature}")
            all_timestamps = [
                float(ts)
                for user_logs in feature_logs.values()
                for ts in (user_logs or [])
            ]
            counts = {
                key: sum(1 for ts in all_timestamps if now - ts <= seconds)
                for key, seconds in RESTRICT_STATS_WINDOWS.items()
            }
            items.append(
                {
                    "resource": feature,
                    "limit_count": int(rule.get("limit", 0) or 0),
                    "window_unit": str(rule.get("unit", "") or ""),
                    "window_seconds": int(rule.get("window", 0) or 0),
                    "counts": counts,
                }
            )
    return items


def export_all() -> dict:
    """整份状态，JSON 文件的格式（迁移和对账用）。"""
    if _db_store is None:
        _load()
        return json.loads(json.dumps(_cache))
    from control_api.models import CooldownEntry
    from sqlalchemy import select

    out: dict = {}
    with _db_store.engine.connect() as conn:
        for section, key, value in conn.execute(select(CooldownEntry.section, CooldownEntry.key, CooldownEntry.value)):
            if section.startswith("restrict_log:"):
                out.setdefault("restrict_log", {}).setdefault(section.split(":", 1)[1], {})[key] = json.loads(value)
            else:
                out.setdefault(section, {})[key] = json.loads(value)
    return out
