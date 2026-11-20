"""Copy control.db (SQLite) and the bot's data/*.json into another database (PostgreSQL), then verify.

    python -m control_api.migrate_to_db --sqlite data/control.db --data-dir data \
        --target "$DATABASE_URL" [--truncate] [--verify-only]

The source files are only read. Without --truncate the target must be empty.
Every table is checked afterwards: row count, sum/max of ids and an md5 over the ids and a
text column; the JSON state is checked by entry counts, point totals and key hashes.
Exit code 0 = everything matches, 1 = mismatch, 2 = refused (target not empty).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import Boolean, DateTime, Float, Integer, delete, func, select, text

from .db import create_app_engine, init_db
from .models import (
    AuditLog,
    Announcement,
    BotAd,
    BotMention,
    CommandLog,
    CooldownEntry,
    EmailBinding,
    IdentityLink,
    OutboundLog,
    PendingGroupReward,
    PointsAccount,
    QQBind,
    ResourceLimitRule,
    ResourceUsage,
    Role,
    UpdatePackage,
    User,
)

CONTROL_MODELS = [
    User, EmailBinding, Role, ResourceLimitRule, ResourceUsage, CommandLog, AuditLog, OutboundLog,
    Announcement, UpdatePackage,
]
STATE_MODELS = [PointsAccount, IdentityLink, QQBind, PendingGroupReward, BotMention, BotAd, CooldownEntry]
# a text column per table for the content checksum
CHECK_COLUMN = {
    "users": "user_key", "email_bindings": "email", "roles": "user_key", "resource_limit_rules": "resource",
    "resource_usage": "user_key", "command_logs": "content", "audit_logs": "action", "outbound_logs": "content",
    "announcements": "title", "update_packages": "title",
}
BATCH = 5000


def _parse_dt(value):
    if value is None or isinstance(value, datetime):
        dt = value
    else:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if dt is not None and dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _converter(column):
    if isinstance(column.type, DateTime):
        return _parse_dt
    if isinstance(column.type, Boolean):
        return lambda v: None if v is None else bool(v)
    if isinstance(column.type, Integer):
        return lambda v: None if v is None else int(v)
    if isinstance(column.type, Float):
        return lambda v: None if v is None else float(v)
    return lambda v: v


def _insert_rows(engine, table, columns, rows) -> None:
    if not rows:
        return
    if engine.dialect.name == "postgresql":
        raw = engine.raw_connection()
        try:
            with raw.cursor() as cur:
                with cur.copy(f"COPY {table.name} ({', '.join(columns)}) FROM STDIN") as copy:
                    for row in rows:
                        copy.write_row(row)
            raw.commit()
        finally:
            raw.close()
    else:
        with engine.begin() as conn:
            conn.execute(table.insert(), [dict(zip(columns, row)) for row in rows])


def copy_control_tables(src: sqlite3.Connection, engine, log) -> dict:
    counts = {}
    for model in CONTROL_MODELS:
        table = model.__table__
        have = {row[1] for row in src.execute(f"PRAGMA table_info({table.name})")}
        if not have:
            counts[table.name] = 0
            continue
        columns = [c.name for c in table.columns if c.name in have]
        convert = [_converter(table.columns[c]) for c in columns]
        cursor = src.execute(f"SELECT {', '.join(columns)} FROM {table.name} ORDER BY id")
        total = 0
        while True:
            batch = cursor.fetchmany(BATCH)
            if not batch:
                break
            _insert_rows(engine, table, columns, [tuple(f(v) for f, v in zip(convert, row)) for row in batch])
            total += len(batch)
        counts[table.name] = total
        log(f"  {table.name}: {total} rows")
    if engine.dialect.name == "postgresql":
        with engine.begin() as conn:
            for model in CONTROL_MODELS + [PendingGroupReward]:
                name = model.__tablename__
                conn.execute(text(
                    f"SELECT setval(pg_get_serial_sequence('{name}', 'id'), "
                    f"COALESCE((SELECT MAX(id) FROM {name}), 0) + 1, false)"
                ))
    return counts


def _read_json(path: Path, default):
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)  # a broken file must stop the migration, not become {}


def load_state(data_dir: Path) -> dict:
    return {
        "sign": _read_json(data_dir / "sign_official.json", {}),
        "identity": _read_json(data_dir / "identity_map.json", {}),
        "qq_binds": _read_json(data_dir / "qq_binds.json", {}),
        "ads": _read_json(data_dir / "ads.json", []),
        "cooldowns": _read_json(data_dir / "cooldowns.json", {}),
    }


def copy_state(state: dict, engine, log) -> dict:
    import state_db

    with engine.begin() as conn:
        accounts = [state_db.account_values(k, v) for k, v in state["sign"].items() if isinstance(v, dict)]
        for i in range(0, len(accounts), BATCH):
            conn.execute(PointsAccount.__table__.insert(), accounts[i:i + BATCH])
        identity = state["identity"]
        links = [{"scenario_openid": k, "union_openid": v} for k, v in identity.items()
                 if not k.startswith("_") and isinstance(v, str)]
        if links:
            conn.execute(IdentityLink.__table__.insert(), links)
        pending = [
            {"group_openid": p.get("group", ""), "member_openid": p.get("member", ""),
             "points": int(p.get("points", 0) or 0), "ts": int(p.get("ts", 0) or 0),
             "notified": bool(p.get("notified", False))}
            for p in identity.get("_pending", []) if isinstance(p, dict)
        ]
        if pending:
            conn.execute(PendingGroupReward.__table__.insert(), pending)
        mentions = [{"group_openid": k, "mention_id": v} for k, v in (identity.get("_bot_mentions") or {}).items()]
        if mentions:
            conn.execute(BotMention.__table__.insert(), mentions)
        binds = [{"limit_user_id": k, "qq": str(v)} for k, v in state["qq_binds"].items() if v]
        if binds:
            conn.execute(QQBind.__table__.insert(), binds)
        ads = [
            {"id": int(a.get("id", 0) or 0), "content": str(a.get("content", "")), "enabled": bool(a.get("enabled")),
             "active_until": a.get("active_until"), "created_at": a.get("created_at")}
            for a in state["ads"] if isinstance(a, dict) and a.get("content")
        ]
        if ads:
            conn.execute(BotAd.__table__.insert(), ads)
        entries = []
        for section, values in state["cooldowns"].items():
            if not isinstance(values, dict):
                continue
            if section == "restrict_log":
                for feature, users in values.items():
                    for key, value in (users or {}).items():
                        entries.append({"section": f"restrict_log:{feature}", "key": str(key), "value": json.dumps(value)})
            else:
                for key, value in values.items():
                    entries.append({"section": section, "key": str(key), "value": json.dumps(value)})
        for i in range(0, len(entries), BATCH):
            conn.execute(CooldownEntry.__table__.insert(), entries[i:i + BATCH])
    counts = {"points_accounts": len(accounts), "identity_links": len(links), "pending_group_rewards": len(pending),
              "bot_mentions": len(mentions), "qq_binds": len(binds), "bot_ads": len(ads),
              "cooldown_entries": len(entries)}
    for name, n in counts.items():
        log(f"  {name}: {n} rows")
    return counts


# ---------------------------------------------------------------------------
# verification
# ---------------------------------------------------------------------------


def _md5_rows(rows) -> str:
    h = hashlib.md5()
    for row_id, value in rows:
        h.update(f"{row_id}:{'' if value is None else value}\n".encode("utf-8"))
    return h.hexdigest()


def control_fingerprint_sqlite(src: sqlite3.Connection) -> dict:
    out = {}
    for model in CONTROL_MODELS:
        name = model.__tablename__
        if not src.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone():
            out[name] = (0, 0, 0, _md5_rows([]))
            continue
        n, s, m = src.execute(f"SELECT COUNT(*), COALESCE(SUM(id),0), COALESCE(MAX(id),0) FROM {name}").fetchone()
        col = CHECK_COLUMN[name]
        digest = _md5_rows(src.execute(f"SELECT id, {col} FROM {name} ORDER BY id"))
        out[name] = (int(n), int(s), int(m), digest)
    return out


def control_fingerprint_target(engine) -> dict:
    out = {}
    with engine.connect() as conn:
        for model in CONTROL_MODELS:
            table = model.__table__
            n, s, m = conn.execute(select(func.count(), func.coalesce(func.sum(table.c.id), 0),
                                          func.coalesce(func.max(table.c.id), 0))).one()
            col = table.c[CHECK_COLUMN[table.name]]
            rows = conn.execute(select(table.c.id, col).order_by(table.c.id)).yield_per(BATCH)
            out[table.name] = (int(n), int(s), int(m), _md5_rows(rows))
    return out


def state_fingerprint_json(state: dict) -> dict:
    sign = {k: v for k, v in state["sign"].items() if isinstance(v, dict)}
    identity = state["identity"]
    cooldown_keys = []
    for section, values in state["cooldowns"].items():
        if not isinstance(values, dict):
            continue
        if section == "restrict_log":
            cooldown_keys += [f"restrict_log:{f}/{k}" for f, users in values.items() for k in (users or {})]
        else:
            cooldown_keys += [f"{section}/{k}" for k in values]
    return {
        "accounts": len(sign),
        "points_total": sum(int(v.get("points", 0) or 0) for v in sign.values()),
        "account_keys_md5": hashlib.md5("\n".join(sorted(sign)).encode()).hexdigest(),
        "accounts_md5": hashlib.md5(json.dumps({k: sign[k] for k in sorted(sign)}, sort_keys=True, ensure_ascii=False).encode()).hexdigest(),
        "identity_links": sum(1 for k, v in identity.items() if not k.startswith("_") and isinstance(v, str)),
        "pending": len([p for p in identity.get("_pending", []) if isinstance(p, dict)]),
        "mentions": len(identity.get("_bot_mentions") or {}),
        "qq_binds": len([v for v in state["qq_binds"].values() if v]),
        "ads": len([a for a in state["ads"] if isinstance(a, dict) and a.get("content")]),
        "cooldown_entries": len(cooldown_keys),
        "cooldown_keys_md5": hashlib.md5("\n".join(sorted(cooldown_keys)).encode()).hexdigest(),
    }


def state_fingerprint_target(engine) -> dict:
    import state_db

    with engine.connect() as conn:
        rows = conn.execute(select(PointsAccount.__table__)).all()
        sign = {row.account_key: state_db.account_from_row(row) for row in rows}
        cooldown_keys = [f"{s}/{k}" for s, k in conn.execute(select(CooldownEntry.section, CooldownEntry.key))]
        count = lambda model: int(conn.execute(select(func.count()).select_from(model)).scalar() or 0)  # noqa: E731
        return {
            "accounts": len(sign),
            "points_total": sum(int(v.get("points", 0) or 0) for v in sign.values()),
            "account_keys_md5": hashlib.md5("\n".join(sorted(sign)).encode()).hexdigest(),
            "accounts_md5": hashlib.md5(json.dumps({k: sign[k] for k in sorted(sign)}, sort_keys=True, ensure_ascii=False).encode()).hexdigest(),
            "identity_links": count(IdentityLink),
            "pending": count(PendingGroupReward),
            "mentions": count(BotMention),
            "qq_binds": count(QQBind),
            "ads": count(BotAd),
            "cooldown_entries": len(cooldown_keys),
            "cooldown_keys_md5": hashlib.md5("\n".join(sorted(cooldown_keys)).encode()).hexdigest(),
        }


def _target_is_empty(engine) -> bool:
    with engine.connect() as conn:
        for model in CONTROL_MODELS + STATE_MODELS:
            if conn.execute(select(func.count()).select_from(model)).scalar():
                return False
    return True


def truncate_target(engine) -> None:
    names = [m.__tablename__ for m in CONTROL_MODELS + STATE_MODELS]
    with engine.begin() as conn:
        if engine.dialect.name == "postgresql":
            conn.execute(text(f"TRUNCATE {', '.join(names)} RESTART IDENTITY"))
        else:
            for model in CONTROL_MODELS + STATE_MODELS:
                conn.execute(delete(model))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--sqlite", required=True, help="source control.db (read-only)")
    parser.add_argument("--data-dir", required=True, help="source data/ dir with the bot JSON files (read-only)")
    parser.add_argument("--target", default=os.getenv("TARGET_DATABASE_URL", ""), help="target SQLAlchemy URL")
    parser.add_argument("--truncate", action="store_true", help="empty the target tables first")
    parser.add_argument("--verify-only", action="store_true")
    args = parser.parse_args(argv)
    if not args.target:
        parser.error("--target (or TARGET_DATABASE_URL) is required")

    log = lambda msg: print(msg, flush=True)  # noqa: E731
    started = time.monotonic()
    src = sqlite3.connect(f"file:{args.sqlite}?mode=ro", uri=True)
    state = load_state(Path(args.data_dir))
    engine = create_app_engine(args.target, statement_timeout_ms=0)
    init_db(engine)

    if not args.verify_only:
        if args.truncate:
            truncate_target(engine)
        elif not _target_is_empty(engine):
            log("REFUSED: target is not empty (use --truncate to replace its contents)")
            return 2
        log("copy control tables:")
        copy_control_tables(src, engine, log)
        log("copy bot state:")
        copy_state(state, engine, log)
        log(f"copied in {time.monotonic() - started:.1f}s")

    ok = True
    log("verify control tables (rows, sum(id), max(id), md5):")
    a, b = control_fingerprint_sqlite(src), control_fingerprint_target(engine)
    for name in a:
        same = a[name] == b[name]
        ok &= same
        log(f"  {'OK ' if same else 'BAD'} {name}: sqlite={a[name][:3]} target={b[name][:3]}{'' if same else f' md5 {a[name][3]} != {b[name][3]}'}")
    log("verify bot state:")
    a, b = state_fingerprint_json(state), state_fingerprint_target(engine)
    for key in a:
        same = a[key] == b[key]
        ok &= same
        shown = (a[key], b[key]) if not key.endswith("md5") else ("match" if same else f"{a[key]} != {b[key]}",)
        log(f"  {'OK ' if same else 'BAD'} {key}: {shown}")
    log(f"{'ALL OK' if ok else 'MISMATCH'} in {time.monotonic() - started:.1f}s")
    engine.dispose()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
