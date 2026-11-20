"""广告：bot 和控制台共用一套读写（以前两个进程各写一份代码、不加锁写同一个文件）。

存储：BOT_STATE_STORE=json 时是 data/ads.json（json_state 原子写）；=db 时是 bot_ads 表。
每条广告：{"id", "content", "enabled", "active_until"(unix 秒或 None), "created_at"}。
"""

from __future__ import annotations

import os
import random
import time as _time_mod

import json_state

_BOT_DIR = os.path.dirname(os.path.abspath(__file__))
ADS_FILE = os.path.join(_BOT_DIR, "data", "ads.json")

DEFAULT_ADS = [
    {
        "id": 1,
        "content": "欢迎进入无限免费小号群喵：1097445697",
        "enabled": True,
        "active_until": None,
    }
]

_db_store = None  # state_db.SqlAdsStore when BOT_STATE_STORE=db
_ads: list[dict] = []  # bot 进程里最近一次读到的列表


def use_database(store) -> None:
    global _db_store
    _db_store = store


# ---------------------------------------------------------------------------
# storage (shared by bot and control API)
# ---------------------------------------------------------------------------


def load_ads(path=None, default=None) -> list[dict]:
    """Read the ad list; ``default`` (a copy) when there is no list yet or it cannot be read."""
    if _db_store is not None:
        return _db_store.load()
    path = path or ADS_FILE
    if os.path.exists(path):
        data = json_state.read_json(path, None, label="ads.json")
        if isinstance(data, list):
            return [d for d in data if isinstance(d, dict) and d.get("content")]
    return [dict(item) for item in (default or [])]


def save_ads(ads: list[dict], path=None) -> bool:
    if _db_store is not None:
        return _db_store.save(ads)
    return json_state.write_json(path or ADS_FILE, ads, indent=2, label="ads.json")


def expire_ads_in_place(ads: list[dict]) -> bool:
    now = _time_mod.time()
    changed = False
    for ad in ads:
        until = ad.get("active_until")
        if ad.get("enabled") and isinstance(until, (int, float)) and until <= now:
            ad["enabled"] = False
            changed = True
    return changed


def read_and_expire(path=None, default=None) -> list[dict]:
    ads = load_ads(path, default)
    if expire_ads_in_place(ads):
        save_ads(ads, path)
    return ads


def next_id(ads: list[dict]) -> int:
    return max((int(ad.get("id", 0) or 0) for ad in ads), default=0) + 1


def find_ad(ads: list[dict], selector: str) -> dict | None:
    """By id first, then by exact content (the /ad commands accept both)."""
    for ad in ads:
        if str(ad.get("id")) == selector:
            return ad
    for ad in ads:
        if ad.get("content", "") == selector:
            return ad
    return None


# ---------------------------------------------------------------------------
# bot side (old names kept)
# ---------------------------------------------------------------------------


def init_bot_ads() -> None:
    """Bot startup: load the list; a JSON install without ads.json gets the default ad written once."""
    global _ads
    _ads = load_ads(ADS_FILE, DEFAULT_ADS)
    if _db_store is None and not os.path.exists(ADS_FILE):
        _save_ads()


def _load_ads():
    return load_ads(ADS_FILE, DEFAULT_ADS)


def _save_ads():
    return save_ads(_ads, ADS_FILE)


def _refresh_ads_from_file():
    global _ads
    _ads = _load_ads()
    changed = _expire_ads_in_place(_ads)
    if changed:
        _save_ads()
    return _ads


def _expire_ads_in_place(ads: list[dict]) -> bool:
    return expire_ads_in_place(ads)


def _get_active_ads():
    _refresh_ads_from_file()
    now = _time_mod.time()
    result = []
    changed = False
    for ad in _ads:
        if not ad.get("enabled"):
            continue
        until = ad.get("active_until")
        if isinstance(until, (int, float)) and until <= now:
            ad["enabled"] = False
            changed = True
            continue
        result.append(ad.get("content", ""))
    if changed:
        _save_ads()
    return result


def _append_ads(text):
    ads = _get_active_ads()
    if not ads:
        return text
    ad = random.choice(ads)
    return f"{text}\n\n━━━ 广告 ━━━\n{ad}"


def _append_full_ads_to_email(body: str) -> str:
    ads = _get_active_ads()
    if not ads:
        return body
    lines = ["", "━━━━━━━━━━━━━━", "完整赞助信息"]
    for index, ad in enumerate(ads, start=1):
        lines.append(f"{index}. {ad}")
    return body.rstrip() + "\n\n" + "\n".join(lines)
