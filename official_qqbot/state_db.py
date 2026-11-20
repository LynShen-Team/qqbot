"""Database storage for the bot's own state (BOT_STATE_STORE=db).

Points, identities, QQ binds, pending invite rewards, learned @ids, ads and cooldowns used
to be JSON files rewritten whole on every change. Here every change is a row-level write in
a transaction (row locks on PostgreSQL), so concurrent updates cannot lose points and a
crash cannot leave a half-written file.

The dict shapes handed back are exactly the JSON shapes, so the business logic in
accounts.py / ads.py / shared_cooldown.py is the same for both stores.
"""

from __future__ import annotations

import json
import os
from contextlib import contextmanager

from sqlalchemy import delete, func, select, update
from sqlalchemy.engine import Connection, Engine

from control_api.db import create_app_engine, init_db
from control_api.models import (
    BotAd,
    BotMention,
    CooldownEntry,
    IdentityLink,
    PendingGroupReward,
    PointsAccount,
    QQBind,
)

ACCOUNT_COLUMNS = ("points", "streak", "last", "group_add_last", "group_add_count", "rewarded_groups", "merged_into")


def open_state_engine(url: str | None = None) -> Engine:
    """Engine for the bot process: small pool, short timeouts (a stuck query must not freeze the bot)."""
    url = url or os.getenv("BOT_STATE_DATABASE_URL", "") or os.getenv("DATABASE_URL", "")
    if not url:
        raise RuntimeError("BOT_STATE_STORE=db needs BOT_STATE_DATABASE_URL or DATABASE_URL")
    engine = create_app_engine(url, pool_size=3, max_overflow=5, statement_timeout_ms=5000)
    init_db(engine)
    return engine


# ---------------------------------------------------------------------------
# row <-> JSON-shaped dict
# ---------------------------------------------------------------------------


def account_from_row(row) -> dict:
    account = {"points": int(row.points or 0), "streak": int(row.streak or 0), "last": row.last_sign or ""}
    if row.rewarded_groups is not None:
        account["rewarded_groups"] = json.loads(row.rewarded_groups)
    if row.group_add_last is not None:
        account["group_add_last"] = row.group_add_last
    if row.group_add_count is not None:
        account["group_add_count"] = int(row.group_add_count)
    if row.merged_into is not None:
        account["merged_into"] = row.merged_into
    if row.extra:
        account.update(json.loads(row.extra))
    return account


def account_values(key: str, account: dict) -> dict:
    extra = {k: v for k, v in account.items() if k not in ACCOUNT_COLUMNS}
    rewarded = account.get("rewarded_groups")
    return {
        "account_key": key,
        "points": int(account.get("points", 0) or 0),
        "streak": int(account.get("streak", 0) or 0),
        "last_sign": str(account.get("last", "") or ""),
        "group_add_last": account.get("group_add_last"),
        "group_add_count": None if account.get("group_add_count") is None else int(account.get("group_add_count") or 0),
        "rewarded_groups": None if rewarded is None else json.dumps(rewarded, ensure_ascii=False),
        "merged_into": account.get("merged_into"),
        "extra": json.dumps(extra, ensure_ascii=False) if extra else None,
    }


def pending_from_row(row) -> dict:
    return {
        "group": row.group_openid,
        "member": row.member_openid,
        "points": int(row.points or 0),
        "ts": int(row.ts or 0),
        "notified": bool(row.notified),
        "_id": row.id,
    }


def ad_from_row(row) -> dict:
    ad = {"id": int(row.id), "content": row.content, "enabled": bool(row.enabled), "active_until": row.active_until}
    if row.created_at is not None:
        ad["created_at"] = row.created_at
    return ad


def upsert(conn: Connection, model, values: dict, keys: tuple[str, ...]) -> None:
    if conn.dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert as dialect_insert
    else:
        from sqlalchemy.dialects.sqlite import insert as dialect_insert
    stmt = dialect_insert(model).values(**values)
    updates = {k: stmt.excluded[k] for k in values if k not in keys}
    if updates:
        stmt = stmt.on_conflict_do_update(index_elements=list(keys), set_=updates)
    else:
        stmt = stmt.on_conflict_do_nothing(index_elements=list(keys))
    conn.execute(stmt)


# ---------------------------------------------------------------------------
# accounts / identities / binds / pending rewards / mentions
# ---------------------------------------------------------------------------


class SqlAccountTx:
    def __init__(self, conn: Connection, lock: bool):
        self.conn = conn
        self.lock = lock

    def _select(self, stmt):
        return stmt.with_for_update() if self.lock else stmt

    def account(self, key: str) -> dict | None:
        table = PointsAccount.__table__
        row = self.conn.execute(self._select(select(table).where(table.c.account_key == key))).first()
        return account_from_row(row) if row is not None else None

    def put_account(self, key: str, account: dict) -> None:
        upsert(self.conn, PointsAccount, account_values(key, account), ("account_key",))

    def qq_bind(self, limit_user_id: str) -> str | None:
        return self.conn.execute(
            select(QQBind.qq).where(QQBind.limit_user_id == limit_user_id)
        ).scalar_one_or_none()

    def put_qq_bind(self, limit_user_id: str, qq: str) -> None:
        upsert(self.conn, QQBind, {"limit_user_id": limit_user_id, "qq": str(qq)}, ("limit_user_id",))

    def delete_qq_bind(self, limit_user_id: str) -> bool:
        return self.conn.execute(delete(QQBind).where(QQBind.limit_user_id == limit_user_id)).rowcount > 0

    def union_of(self, scenario_openid: str) -> str | None:
        return self.conn.execute(
            select(IdentityLink.union_openid).where(IdentityLink.scenario_openid == scenario_openid)
        ).scalar_one_or_none()

    def put_union(self, scenario_openid: str, union_openid: str) -> None:
        upsert(
            self.conn, IdentityLink,
            {"scenario_openid": scenario_openid, "union_openid": union_openid}, ("scenario_openid",),
        )

    def is_known_openid(self, openid: str) -> bool:
        return self.union_of(openid) is not None

    def pending(self) -> list[dict]:
        table = PendingGroupReward.__table__
        rows = self.conn.execute(self._select(select(table).order_by(table.c.id))).all()
        return [pending_from_row(row) for row in rows]

    def add_pending(self, item: dict) -> None:
        self.conn.execute(PendingGroupReward.__table__.insert().values(
            group_openid=item["group"], member_openid=item["member"], points=int(item.get("points", 0)),
            ts=int(item.get("ts", 0)), notified=bool(item.get("notified", False)),
        ))

    def mark_pending_notified(self, items: list[dict]) -> None:
        ids = [item["_id"] for item in items if "_id" in item]
        if ids:
            self.conn.execute(update(PendingGroupReward).where(PendingGroupReward.id.in_(ids)).values(notified=True))
        for item in items:
            item["notified"] = True

    def remove_pending(self, items: list[dict]) -> None:
        ids = [item["_id"] for item in items if "_id" in item]
        if ids:
            self.conn.execute(delete(PendingGroupReward).where(PendingGroupReward.id.in_(ids)))

    def mentions(self) -> dict:
        return dict(self.conn.execute(select(BotMention.group_openid, BotMention.mention_id)).all())

    def put_mention(self, group_openid: str, mention_id: str) -> None:
        upsert(self.conn, BotMention, {"group_openid": group_openid, "mention_id": mention_id}, ("group_openid",))

    def top_accounts(self, limit: int) -> list[tuple[str, dict]]:
        table = PointsAccount.__table__
        rows = self.conn.execute(
            select(table).where(table.c.merged_into.is_(None))
            .order_by(table.c.points.desc(), table.c.account_key).limit(limit)
        ).all()
        return [(row.account_key, account_from_row(row)) for row in rows]

    def count_active(self) -> int:
        return int(self.conn.execute(
            select(func.count()).select_from(PointsAccount).where(PointsAccount.merged_into.is_(None))
        ).scalar() or 0)


class SqlAccountStore:
    name = "db"

    def __init__(self, engine: Engine):
        self.engine = engine

    @contextmanager
    def transaction(self, readonly: bool = False):
        with self.engine.begin() as conn:
            yield SqlAccountTx(conn, lock=not readonly)


# ---------------------------------------------------------------------------
# ads
# ---------------------------------------------------------------------------


class SqlAdsStore:
    name = "db"

    def __init__(self, engine: Engine):
        self.engine = engine

    def load(self) -> list[dict]:
        with self.engine.connect() as conn:
            rows = conn.execute(select(BotAd.__table__).order_by(BotAd.id)).all()
        return [ad_from_row(row) for row in rows]

    def save(self, ads: list[dict]) -> bool:
        """Replace the whole list (it is tiny) in one transaction."""
        with self.engine.begin() as conn:
            ids = [int(ad.get("id", 0) or 0) for ad in ads]
            if ids:
                conn.execute(delete(BotAd).where(BotAd.id.not_in(ids)))
            else:
                conn.execute(delete(BotAd))
            for ad in ads:
                upsert(conn, BotAd, {
                    "id": int(ad.get("id", 0) or 0),
                    "content": str(ad.get("content", "")),
                    "enabled": bool(ad.get("enabled")),
                    "active_until": ad.get("active_until"),
                    "created_at": ad.get("created_at"),
                }, ("id",))
        return True


# ---------------------------------------------------------------------------
# cooldowns
# ---------------------------------------------------------------------------


class SqlCooldownTx:
    def __init__(self, conn: Connection):
        self.conn = conn

    def get(self, section: str, key: str):
        raw = self.conn.execute(
            select(CooldownEntry.value)
            .where(CooldownEntry.section == section, CooldownEntry.key == str(key))
            .with_for_update()
        ).scalar_one_or_none()
        return None if raw is None else json.loads(raw)

    def put(self, section: str, key: str, value) -> None:
        upsert(self.conn, CooldownEntry, {"section": section, "key": str(key), "value": json.dumps(value)}, ("section", "key"))

    def delete(self, section: str, key: str) -> None:
        self.conn.execute(delete(CooldownEntry).where(CooldownEntry.section == section, CooldownEntry.key == str(key)))

    def items(self, section: str) -> dict:
        rows = self.conn.execute(
            select(CooldownEntry.key, CooldownEntry.value).where(CooldownEntry.section == section)
        ).all()
        return {key: json.loads(value) for key, value in rows}

    def clear_prefix(self, prefix: str) -> None:
        self.conn.execute(delete(CooldownEntry).where(CooldownEntry.section.startswith(prefix, autoescape=True)))


class SqlCooldownStore:
    name = "db"

    def __init__(self, engine: Engine):
        self.engine = engine

    @contextmanager
    def transaction(self):
        with self.engine.begin() as conn:
            yield SqlCooldownTx(conn)
