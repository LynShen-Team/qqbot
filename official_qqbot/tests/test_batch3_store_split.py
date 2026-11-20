"""Batch 3: the split modules, the command table, and JSON vs database state stores.

The database tests use a temporary SQLite file by default. Set QQBOT_TEST_DATABASE_URL to a
scratch PostgreSQL database (postgresql+psycopg://...) to run the same tests on PostgreSQL;
they wipe the bot-state tables of that database.
"""

import json
import os
import sqlite3
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from io import StringIO
from unittest.mock import AsyncMock, patch

import accounts
import ads
import bot
import commands
import qq_api
import resources
import shared_cooldown
import state_db
from control_api import migrate_to_db
from control_api.db import create_app_engine, init_db
from control_api.models import CommandLog, User
from sqlalchemy import delete, text

PG_URL = os.environ.get("QQBOT_TEST_DATABASE_URL", "")


def _scratch_engine(tmpdir):
    url = PG_URL or f"sqlite:///{tmpdir}/state.db"
    engine = create_app_engine(url)
    init_db(engine)
    with engine.begin() as conn:
        for model in migrate_to_db.STATE_MODELS:
            conn.execute(delete(model))
    return engine


class _JsonFiles:
    """Point the JSON store at empty temp files and fresh dicts."""

    def __init__(self, tmpdir):
        self.tmpdir = tmpdir

    def __enter__(self):
        self.saved = {n: getattr(accounts, n) for n in ("SIGN_FILE", "IDENTITY_FILE", "QQ_BIND_FILE", "_sign_data", "_identity_map", "_qq_binds")}
        accounts.SIGN_FILE = os.path.join(self.tmpdir, "sign_official.json")
        accounts.IDENTITY_FILE = os.path.join(self.tmpdir, "identity_map.json")
        accounts.QQ_BIND_FILE = os.path.join(self.tmpdir, "qq_binds.json")
        accounts._sign_data, accounts._identity_map, accounts._qq_binds = {}, {}, {}
        return self

    def __exit__(self, *exc):
        for n, v in self.saved.items():
            setattr(accounts, n, v)


def _scenario():
    """Same sequence of account operations; returns the replies."""
    out = []
    out.append(accounts.do_sign_in("u1"))
    out.append(accounts.do_sign_in("u1"))
    out.append(accounts.try_spend_points("u1", 20))
    out.append(accounts.try_spend_points("u1", 999))
    out.append(accounts.award_group_add_points("u1", "g1"))
    out.append(accounts.award_group_add_points("u1", "g1"))
    accounts._queue_pending_reward("g2", "member-x")
    accounts._queue_pending_reward("g2", "member-x")
    out.append(accounts._flush_pending_rewards("g2", "member-x", "member-x"))
    out.append(accounts._learn_identity("scene-1", "union-1"))
    accounts.do_sign_in("scene-1")
    accounts._merge_legacy_points("union-1", ["scene-1"])
    out.append(accounts.get_user_points("union-1"))
    out.append(accounts.bind_qq_account("u1", "123456789"))
    out.append(accounts.bind_qq_account("u2", "123456789"))
    out.append(accounts._account_key("u1"))
    accounts.learn_bot_mention("g1", "BOTID")
    out.append(accounts.bot_mention_ids())
    out.append(accounts.is_known_member_openid("scene-1"))
    out.append(accounts.leaderboard_text())
    out.append(accounts.points_balance_text("QQ123456789"))
    out.append(accounts.unbind_qq_account("u2"))
    out.append(accounts.qq_bind_of("u2"))
    return out


def _export_accounts():
    with accounts._tx(readonly=True) as tx:
        top = tx.top_accounts(1000)
        pending = [{k: v for k, v in item.items() if k != "_id"} for item in tx.pending()]
        return {
            "accounts": dict(top),
            "binds": {u: tx.qq_bind(u) for u in ("u1", "u2", "union-1")},
            "pending": pending,
            "mentions": tx.mentions(),
            "active": tx.count_active(),
        }


class AccountStoreEquivalenceTests(unittest.TestCase):
    def test_json_and_database_stores_behave_the_same(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _JsonFiles(tmp):
                json_replies = _scenario()
                json_state = _export_accounts()
                with open(accounts.SIGN_FILE, encoding="utf-8") as f:
                    self.assertEqual(json.load(f)["QQ123456789"]["points"], json_state["accounts"]["QQ123456789"]["points"])
            engine = _scratch_engine(tmp)
            accounts.use_database(state_db.SqlAccountStore(engine))
            try:
                db_replies = _scenario()
                db_state = _export_accounts()
            finally:
                accounts.use_database(None)
                engine.dispose()
        self.assertEqual(db_replies, json_replies)
        self.assertEqual(db_state, json_state)
        self.assertEqual(json_state["binds"]["u2"], None)
        self.assertGreater(json_state["accounts"]["QQ123456789"]["points"], 0)

    def test_account_rows_round_trip_unknown_keys(self):
        account = {"points": 7, "streak": 2, "last": "2026-11-20", "rewarded_groups": ["g"],
                   "group_add_last": "2026-11-20", "group_add_count": 1, "merged_into": "QQ1", "nickname": "x"}
        values = state_db.account_values("k", account)

        class Row:
            pass

        row = Row()
        for column, value in values.items():
            setattr(row, column, value)
        row.last_sign = values["last_sign"]
        self.assertEqual(state_db.account_from_row(row), account)


@unittest.skipUnless(PG_URL, "needs QQBOT_TEST_DATABASE_URL (PostgreSQL)")
class PostgresConcurrencyTests(unittest.TestCase):
    def test_concurrent_spends_never_overdraw(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine = _scratch_engine(tmp)
            accounts.use_database(state_db.SqlAccountStore(engine))
            try:
                accounts.award_group_add_points("rich", "g1")  # 25 points
                results = []

                def spend():
                    results.append(accounts.try_spend_points("rich", 5)[0])

                threads = [threading.Thread(target=spend) for _ in range(20)]
                for t in threads:
                    t.start()
                for t in threads:
                    t.join()
                self.assertEqual(results.count(True), 5)
                self.assertEqual(accounts.get_user_points("rich"), 0)
            finally:
                accounts.use_database(None)
                engine.dispose()


def _cooldown_scenario():
    with patch.object(shared_cooldown.time, "time", return_value=1000.0):
        shared_cooldown.record_usage("163", "a")
        shared_cooldown.record_rate_usage("sauth", "a")
        r1 = shared_cooldown.check_rate_limit("sauth", "a", 1, 60)
        r2 = shared_cooldown.check_cooldown("163", "a", 3600)
        for _ in range(5):
            shared_cooldown.record_usage("nfa", "b")
        r3 = shared_cooldown.check_hour_limit("nfa", "b", 5, 86400)
        r4 = shared_cooldown.is_banned("nfa", "b")
        shared_cooldown.set_restrict_rule("163", 2, "min")
        shared_cooldown.record_restrict_usage("163", "a")
        r5 = shared_cooldown.get_restrict_status("163", "a")
        r6 = shared_cooldown.get_restrict_usage_stats()
    with patch.object(shared_cooldown.time, "time", return_value=1000.0 + 90000):
        r7 = shared_cooldown.is_banned("nfa", "b")
    shared_cooldown.reset_restrict_usage()
    return [r1, r2, r3, r4, r5, r6, r7], shared_cooldown.export_all()


class CooldownStoreEquivalenceTests(unittest.TestCase):
    def test_json_and_database_cooldowns_behave_the_same(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_file = shared_cooldown.SHARED_FILE
            shared_cooldown.SHARED_FILE = os.path.join(tmp, "cooldowns.json")
            shared_cooldown._cache, shared_cooldown._last_load_ts = {}, 0
            try:
                json_results, json_export = _cooldown_scenario()
            finally:
                shared_cooldown.SHARED_FILE = old_file
                shared_cooldown._cache, shared_cooldown._last_load_ts = {}, 0
            engine = _scratch_engine(tmp)
            shared_cooldown.use_database(state_db.SqlCooldownStore(engine))
            try:
                db_results, db_export = _cooldown_scenario()
            finally:
                shared_cooldown.use_database(None)
                engine.dispose()
        self.assertEqual(db_results, json_results)
        drop_empty = lambda d: {k: v for k, v in d.items() if v}  # noqa: E731
        self.assertEqual(drop_empty(db_export), drop_empty(json_export))


class AdsStoreTests(unittest.TestCase):
    def test_database_ads_store_matches_file_semantics(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine = _scratch_engine(tmp)
            ads.use_database(state_db.SqlAdsStore(engine))
            try:
                self.assertEqual(ads.load_ads(default=ads.DEFAULT_ADS), [])  # no default rows in the database
                items = [{"id": 1, "content": "a", "enabled": True, "active_until": 1.0, "created_at": "x"},
                         {"id": 2, "content": "b", "enabled": True, "active_until": None}]
                ads.save_ads(items)
                expired = ads.read_and_expire()
                self.assertEqual([(a["id"], a["enabled"]) for a in expired], [(1, False), (2, True)])
                self.assertEqual(ads.next_id(expired), 3)
                ads.save_ads([expired[1]])
                self.assertEqual([a["id"] for a in ads.load_ads()], [2])
            finally:
                ads.use_database(None)
                engine.dispose()


class SplitCompatibilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_patching_through_bot_reaches_every_module(self):
        with patch.object(bot, "reply", new_callable=AsyncMock) as mocked:
            self.assertIs(commands.reply, mocked)
            self.assertIs(resources.reply, mocked)
            self.assertIs(qq_api.reply, mocked)
        self.assertIsNot(commands.reply, mocked)
        self.assertIs(commands.reply, qq_api.reply)

    def test_assigning_state_through_bot_is_seen_by_the_owner_module(self):
        old = accounts._sign_data
        try:
            bot._sign_data = {"x": {"points": 3, "streak": 0, "last": ""}}
            self.assertEqual(accounts.get_user_points("x"), 3)
        finally:
            bot._sign_data = old

    def test_every_split_file_stays_under_600_lines(self):
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        for name in ("bot.py", "config.py", "control_state.py", "qq_api.py", "accounts.py", "ads.py", "mailer.py",
                     "resources.py", "commands.py", "commands_admin.py", "messages.py", "ingress.py"):
            with self.subTest(name=name), open(os.path.join(here, name), encoding="utf-8") as f:
                self.assertLessEqual(len(f.readlines()), 600)

    def test_command_table_keeps_the_old_order(self):
        names = [c.name for c in commands.COMMANDS]
        self.assertLess(names.index("help"), names.index("ban-gate"))
        self.assertLess(names.index("mdtest"), names.index("ban-gate"))
        self.assertEqual(names.index("ban-gate"), 2)
        self.assertLess(names.index("bindqq"), names.index("bind-email"))
        self.assertEqual(names[-1], "ad")

    async def test_banned_user_still_gets_help_but_nothing_else(self):
        ctx = {"type": "c2c", "user_openid": "banned-user", "msg_id": "m", "limit_user_id": "banned-user"}
        with patch.object(bot._state_backend, "is_banned", return_value=True), patch.object(
            bot, "reply", new_callable=AsyncMock
        ) as reply, patch.object(bot, "reply_plain", new_callable=AsyncMock) as reply_plain:
            await bot.handle_command(ctx, "/help")
            self.assertIn("指令列表", reply.await_args.args[1])
            await bot.handle_command(ctx, "签到")
            self.assertIn("已被封禁", reply_plain.await_args.args[1])

    async def test_one_resource_flow_for_4399_sauth_163(self):
        self.assertEqual(set(resources.RESOURCE_SPECS), {"4399", "sauth", "163"})
        self.assertEqual(
            [g.__name__ for g in resources.RESOURCE_SPECS["163"].gates],
            ["_gate_restrict", "_gate_163_ban", "_gate_163_cooldown"],
        )

    async def test_empty_song_name_falls_through_like_the_old_if_chain(self):
        ctx = {"type": "c2c", "user_openid": "u", "msg_id": "m", "limit_user_id": "u"}
        with patch.object(bot, "reply", new_callable=AsyncMock), patch.object(bot.music, "search_music", new_callable=AsyncMock) as search:
            handled = await bot.handle_command(ctx, "点歌 ：")
        search.assert_not_awaited()
        self.assertFalse(handled)


class MigrationTests(unittest.TestCase):
    def _source(self, tmp):
        engine = create_app_engine(f"sqlite:///{tmp}/control.db")
        init_db(engine)
        with engine.begin() as conn:
            conn.execute(User.__table__.insert(), [{"user_key": f"u{i}", "is_banned": i == 3} for i in range(10)])
            conn.execute(CommandLog.__table__.insert(), [{"user_key": "u1", "command": "/x", "content": f"c{i}"} for i in range(25)])
        engine.dispose()
        data = os.path.join(tmp, "data")
        os.makedirs(data)
        files = {
            "sign_official.json": {"a": {"points": 5, "streak": 1, "last": "2026-11-20"},
                                    "QQ1": {"points": 9, "streak": 0, "last": "", "rewarded_groups": ["g"],
                                            "group_add_last": "2026-11-20", "group_add_count": 1},
                                    "b": {"points": 0, "streak": 0, "last": "", "merged_into": "QQ1"}},
            "identity_map.json": {"s": "u", "_pending": [{"group": "g", "member": "m", "points": 25, "ts": 1, "notified": False}],
                                  "_bot_mentions": {"g": "BOT"}},
            "qq_binds.json": {"b": "1"},
            "ads.json": [{"id": 4, "content": "ad", "enabled": True, "active_until": None}],
            "cooldowns.json": {"163_cd": {"a": 1.5}, "sauth_rate": {"a": [1.0, 2.0]},
                               "restrict_log": {"163": {"a": [3.0]}}, "restrict_rules": {"nfa": {"limit": 1, "unit": "year", "window": 31536000}}},
        }
        for name, value in files.items():
            with open(os.path.join(data, name), "w", encoding="utf-8") as f:
                json.dump(value, f)
        return f"{tmp}/control.db", data

    def _run(self, *args):
        out = StringIO()
        with redirect_stdout(out):
            code = migrate_to_db.main(list(args))
        return code, out.getvalue()

    def test_copy_verify_refuse_and_detect_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            src, data = self._source(tmp)
            target = PG_URL or f"sqlite:///{tmp}/target.db"
            code, out = self._run("--sqlite", src, "--data-dir", data, "--target", target, "--truncate")
            self.assertEqual(code, 0, out)
            self.assertIn("ALL OK", out)
            self.assertIn("users: 10 rows", out)
            self.assertIn("command_logs: 25 rows", out)
            self.assertIn("cooldown_entries: 4 rows", out)

            code, out = self._run("--sqlite", src, "--data-dir", data, "--target", target)
            self.assertEqual(code, 2, out)

            engine = create_app_engine(target)
            with engine.begin() as conn:
                conn.execute(text("UPDATE command_logs SET content = 'changed' WHERE id = 7"))
            engine.dispose()
            code, out = self._run("--sqlite", src, "--data-dir", data, "--target", target, "--verify-only")
            self.assertEqual(code, 1, out)
            self.assertIn("BAD command_logs", out)

            # the migrated state is readable by the bot's database store
            engine = create_app_engine(target)
            accounts.use_database(state_db.SqlAccountStore(engine))
            try:
                self.assertEqual(accounts.get_user_points("QQ1"), 9)
                self.assertEqual(accounts._account_key("b"), "QQ1")
                self.assertEqual(accounts.bot_mention_ids(), {"g": "BOT"})
            finally:
                accounts.use_database(None)
                engine.dispose()
            # sources untouched
            self.assertEqual(sqlite3.connect(src).execute("SELECT COUNT(*) FROM command_logs").fetchone()[0], 25)


if __name__ == "__main__":
    unittest.main()
