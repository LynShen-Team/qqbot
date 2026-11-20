"""Mask the account/password text already stored in outbound_logs (rows are kept, only content changes).

    python -m control_api.redact_logs [--database-url URL] [--dry-run]

Uses redact.redact_outbound(), the same function the bot now applies before writing a send log,
so old and new rows read the same: "[发号] 4399 账号（已脱敏：账号密码不记录）". Channel, status,
user, message id and time are unchanged. Idempotent; prints counts by type and re-checks that no
row with a credential marker is left.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections import Counter

from sqlalchemy import bindparam, or_, select, update

from redact import contains_secret, redact_outbound, resource_label

from .db import create_app_engine
from .models import OutboundLog

_LIKE_MARKERS = ("%密码：%", "%密码:%", "%Token：%", "%Token:%", "%Sauth：%", "%Sauth:%", "%来了喵%")
BATCH = 2000


def _candidates(conn):
    stmt = select(OutboundLog.id, OutboundLog.content).where(
        or_(*[OutboundLog.content.like(marker) for marker in _LIKE_MARKERS])
    ).order_by(OutboundLog.id)
    return [(row_id, content) for row_id, content in conn.execute(stmt) if contains_secret(content)]


def redact_outbound_logs(engine, dry_run: bool = False) -> dict:
    with engine.connect() as conn:
        rows = _candidates(conn)
    by_type = Counter(resource_label(content) or "其它含密码" for _, content in rows)
    changes = [{"row_id": row_id, "new_content": redact_outbound(content)} for row_id, content in rows]
    if changes and not dry_run:
        stmt = (
            update(OutboundLog.__table__)
            .where(OutboundLog.__table__.c.id == bindparam("row_id"))
            .values(content=bindparam("new_content"))
        )
        with engine.begin() as conn:
            for i in range(0, len(changes), BATCH):
                conn.execute(stmt, changes[i:i + BATCH])
    with engine.connect() as conn:
        remaining = len(_candidates(conn))
    return {"matched": len(rows), "updated": 0 if dry_run else len(changes), "by_type": dict(by_type),
            "remaining_with_secrets": remaining}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--database-url", default=os.getenv("DATABASE_URL", ""))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    if not args.database_url:
        parser.error("--database-url or DATABASE_URL is required")
    engine = create_app_engine(args.database_url, statement_timeout_ms=0)
    try:
        result = redact_outbound_logs(engine, dry_run=args.dry_run)
    finally:
        engine.dispose()
    print(f"matched={result['matched']} updated={result['updated']} "
          f"remaining_with_secrets={result['remaining_with_secrets']} by_type={result['by_type']}", flush=True)
    return 0 if (args.dry_run or result["remaining_with_secrets"] == 0) else 1


if __name__ == "__main__":
    sys.exit(main())
