"""Regression tests for the 2026-11 review fixes (security, bugs, speed)."""

import asyncio
import importlib
import json
import os
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, Mock, patch

import bot
import json_state
import shared_cooldown
from control_api.db import create_app_engine, init_db
from control_api.service import ControlService
from handlers import email_sender, fun, sauth
from state_backend import CloudStateBackend


class _BotStateTestBase(unittest.IsolatedAsyncioTestCase):
    user_id = "user-openid-0001"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = {
            name: getattr(bot, name)
            for name in (
                "SIGN_FILE", "QQ_BIND_FILE", "IDENTITY_FILE", "ADS_FILE", "_sign_data",
                "_qq_binds", "_identity_map", "_ads", "_state_backend", "ADMIN_SECRET",
                "QQ_BIND_VERIFY",
            )
        }
        self.old_admins = set(bot._admin_set)
        self.old_shared_file = shared_cooldown.SHARED_FILE
        bot.SIGN_FILE = os.path.join(self.tmp.name, "sign_official.json")
        bot.QQ_BIND_FILE = os.path.join(self.tmp.name, "qq_binds.json")
        bot.IDENTITY_FILE = os.path.join(self.tmp.name, "identity_map.json")
        bot.ADS_FILE = os.path.join(self.tmp.name, "ads.json")
        shared_cooldown.SHARED_FILE = os.path.join(self.tmp.name, "cooldowns.json")
        shared_cooldown._cache = {}
        shared_cooldown._last_load_ts = 0
        bot._sign_data = {}
        bot._qq_binds = {}
        bot._identity_map = {}
        bot._qq_bind_codes.clear()
        bot._active_resource_request_keys.clear()
        self.ctx = {
            "type": "c2c",
            "user_openid": self.user_id,
            "msg_id": "msg-1",
            "limit_user_id": self.user_id,
        }

    def tearDown(self):
        for name, value in self.saved.items():
            setattr(bot, name, value)
        bot._admin_set.clear()
        bot._admin_set.update(self.old_admins)
        bot._qq_bind_codes.clear()
        bot._active_resource_request_keys.clear()
        shared_cooldown.SHARED_FILE = self.old_shared_file
        shared_cooldown._cache = {}
        shared_cooldown._last_load_ts = 0
        self.tmp.cleanup()


class SecretsTests(unittest.TestCase):
    def test_inventory_keys_come_from_environment(self):
        with patch.dict(os.environ, {"SAUTH_API_KEY": "env-key", "SAUTH_ADMIN_TOKEN": "env-admin"}):
            module = importlib.reload(sauth)
            self.assertEqual(module.SAUTH_API_KEY, "env-key")
            self.assertEqual(module.SAUTH_ADMIN_TOKEN, "env-admin")
        importlib.reload(sauth)

    def test_no_inventory_key_literals_in_source(self):
        source = open(sauth.__file__, encoding="utf-8").read()
        self.assertNotRegex(source, r'SAUTH_API_KEY = "[^"]+"')
        self.assertNotRegex(source, r'SAUTH_ADMIN_TOKEN = "[^"]+"')
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        sources = ""
        for dirpath, _dirs, files in os.walk(root):
            if os.sep + "tests" in dirpath or ".venv" in dirpath:
                continue
            for name in files:
                if name.endswith(".py"):
                    with open(os.path.join(dirpath, name), encoding="utf-8") as f:
                        sources += f.read()
        self.assertIn("NFA_PASSWORD", sources)
        self.assertNotRegex(sources, r'get_nfa_token\("[^"]*", "[^"]*"\)')
        self.assertNotIn("miracle2026", sources)


class AuthTests(_BotStateTestBase):
    async def test_auth_prompt_does_not_show_a_sample_passphrase(self):
        with patch.object(bot, "reply", new_callable=AsyncMock) as reply:
            await bot.handle_command(self.ctx, "/auth")
        self.assertEqual(reply.await_args.args[1], "请发送：/auth 验证码")

    async def test_unconfigured_admin_secret_accepts_nothing(self):
        bot.ADMIN_SECRET = ""
        with patch.object(bot, "reply_plain", new_callable=AsyncMock) as reply_plain:
            await bot.handle_command(self.ctx, "/auth miracle2026")
        self.assertFalse(bot._is_admin(self.user_id))
        self.assertEqual(reply_plain.await_args.args[1], "验证码不正确喵~")

    async def test_configured_admin_secret_still_works(self):
        bot.ADMIN_SECRET = "s3cret-pass"
        with patch.object(bot, "reply_plain", new_callable=AsyncMock):
            await bot.handle_command(self.ctx, "/auth s3cret-pass")
        self.assertTrue(bot._is_admin(self.user_id))

    async def test_nfa_uses_configured_credentials(self):
        with patch.object(bot, "NFA_USERNAME", "nfa-user"), patch.object(
            bot, "NFA_PASSWORD", "nfa-pass"
        ), patch.object(bot.nfa, "get_nfa_token", new_callable=AsyncMock) as get_token, patch.object(
            bot, "reply", new_callable=AsyncMock
        ), patch.object(
            bot._state_backend, "get_resource_limit_status", return_value={"blocked": False}
        ):
            get_token.return_value = "失败"
            await bot.handle_command(self.ctx, "/nfa")
        get_token.assert_awaited_once_with("nfa-user", "nfa-pass")


class AdPermissionTests(_BotStateTestBase):
    async def test_regular_user_cannot_add_ads(self):
        bot._ads = [{"id": 1, "content": "existing", "enabled": True, "active_until": None}]
        with patch.object(bot, "reply", new_callable=AsyncMock) as reply, patch.object(
            bot, "reply_plain", new_callable=AsyncMock
        ) as reply_plain:
            handled = await bot.handle_command(self.ctx, "/ad+ buy my stuff")
        self.assertTrue(handled)
        self.assertEqual(reply.await_args.args[1], "需要管理权限喵~")
        reply_plain.assert_not_awaited()
        self.assertFalse(os.path.exists(bot.ADS_FILE))

    async def test_admin_can_add_ads(self):
        bot._admin_set.add(self.user_id)
        json_state.write_json(bot.ADS_FILE, [])
        with patch.object(bot, "reply_plain", new_callable=AsyncMock):
            await bot.handle_command(self.ctx, "/ad+ new ad")
        with open(bot.ADS_FILE, encoding="utf-8") as f:
            self.assertEqual([ad["content"] for ad in json.load(f)], ["new ad"])


class BindQQVerificationTests(_BotStateTestBase):
    async def test_off_binds_directly(self):
        bot.QQ_BIND_VERIFY = "off"
        with patch.object(bot, "reply_plain", new_callable=AsyncMock), patch.object(
            bot, "_send_result_email", new_callable=AsyncMock
        ) as send_email:
            await bot.handle_command(self.ctx, "/bindqq 123456789")
        send_email.assert_not_awaited()
        self.assertEqual(bot._qq_binds[self.user_id], "123456789")

    async def test_email_mode_requires_code_from_qq_mailbox(self):
        bot.QQ_BIND_VERIFY = "email"
        with patch.object(bot, "reply_plain", new_callable=AsyncMock) as reply_plain, patch.object(
            bot, "_send_result_email", new_callable=AsyncMock, return_value=(True, "")
        ) as send_email:
            await bot.handle_command(self.ctx, "/bindqq 123456789")
            self.assertNotIn(self.user_id, bot._qq_binds)
            self.assertEqual(send_email.await_args.args[0], "123456789@qq.com")
            code = bot._qq_bind_codes[self.user_id]["code"]
            self.assertIn(code, send_email.await_args.args[2])
            self.assertNotIn(code, reply_plain.await_args.args[1])

            wrong = "000000" if code != "000000" else "111111"
            await bot.handle_command(self.ctx, f"/bindqq 123456789 {wrong}")
            self.assertNotIn(self.user_id, bot._qq_binds)

            await bot.handle_command(self.ctx, f"/bindqq 123456789 {code}")
        self.assertEqual(bot._qq_binds[self.user_id], "123456789")
        self.assertNotIn(self.user_id, bot._qq_bind_codes)

    async def test_email_mode_limits_mail_to_one_per_minute(self):
        bot.QQ_BIND_VERIFY = "email"
        with patch.object(bot, "reply_plain", new_callable=AsyncMock) as reply_plain, patch.object(
            bot, "_send_result_email", new_callable=AsyncMock, return_value=(True, "")
        ) as send_email:
            await bot.handle_command(self.ctx, "/bindqq 123456789")
            await bot.handle_command(self.ctx, "/bindqq 987654321")
        self.assertEqual(send_email.await_count, 1)
        self.assertIn("太频繁", reply_plain.await_args.args[1])

    async def test_too_many_wrong_codes_discard_the_code(self):
        bot.QQ_BIND_VERIFY = "email"
        with patch.object(bot, "reply_plain", new_callable=AsyncMock), patch.object(
            bot, "_send_result_email", new_callable=AsyncMock, return_value=(True, "")
        ):
            await bot.handle_command(self.ctx, "/bindqq 123456789")
            code = bot._qq_bind_codes[self.user_id]["code"]
            wrong = "000000" if code != "000000" else "111111"
            for _ in range(bot._QQ_BIND_MAX_TRIES):
                await bot.handle_command(self.ctx, f"/bindqq 123456789 {wrong}")
            await bot.handle_command(self.ctx, f"/bindqq 123456789 {code}")
        self.assertNotIn(self.user_id, bot._qq_binds)


class BugFixTests(_BotStateTestBase):
    async def test_jrrp_replies_with_a_value(self):
        with patch.object(bot, "reply", new_callable=AsyncMock) as reply:
            handled = await bot.handle_command(self.ctx, "jrrp")
        self.assertTrue(handled)
        self.assertIn("今日人品", reply.await_args.args[1])

    async def test_weather_is_awaited(self):
        with patch.object(fun, "get_weather", new_callable=AsyncMock, return_value="北京天气\n晴"), patch.object(
            bot, "reply", new_callable=AsyncMock
        ) as reply:
            handled = await bot.check_weather(self.ctx, "北京天气")
        self.assertTrue(handled)
        reply.assert_awaited_once_with(self.ctx, "北京天气\n晴")

    async def test_unbindqq_uses_the_full_target_id(self):
        bot._admin_set.add(self.user_id)
        bot._qq_binds = {"target-openid": "123456789"}
        with patch.object(bot, "reply_plain", new_callable=AsyncMock) as reply_plain:
            await bot.handle_command(self.ctx, "/unbindqq target-openid")
        self.assertEqual(bot._qq_binds, {})
        reply_plain.assert_awaited_once()

    async def test_restrict_rule_for_4399_takes_effect(self):
        bot._sign_data = {self.user_id: {"points": 100, "streak": 0, "last": ""}}
        blocked = {"blocked": True, "count": 2, "limit": 2, "window": 86400, "reset_after": 60}
        with patch.object(bot._state_backend, "get_resource_limit_status", return_value=blocked) as status, patch.object(
            bot.sauth, "get_4399_credentials", new_callable=AsyncMock
        ) as fetch, patch.object(bot, "reply", new_callable=AsyncMock) as reply:
            await bot.handle_command(self.ctx, "/4399")
        status.assert_called_once_with("4399", self.user_id)
        fetch.assert_not_awaited()
        self.assertIn("已达到获取上限", reply.await_args.args[1])

    async def test_restrict_rule_for_163_takes_effect(self):
        bot._sign_data = {self.user_id: {"points": 100, "streak": 0, "last": ""}}
        blocked = {"blocked": True, "count": 1, "limit": 1, "window": 86400, "reset_after": 60}
        with patch.object(bot._state_backend, "get_resource_limit_status", return_value=blocked), patch.object(
            bot.sauth, "get_163_credentials", new_callable=AsyncMock
        ) as fetch, patch.object(bot, "reply", new_callable=AsyncMock):
            await bot.handle_command(self.ctx, "/163")
        fetch.assert_not_awaited()

    async def test_busy_flag_is_cleared_when_the_fetch_raises(self):
        bot._sign_data = {self.user_id: {"points": 100, "streak": 0, "last": ""}}
        for command, attr in (("/4399", "get_4399_credentials"), ("/sauth", "get_sauth"), ("/163", "get_163_credentials")):
            with self.subTest(command=command), patch.object(
                bot.sauth, attr, new_callable=AsyncMock, side_effect=RuntimeError("control api down")
            ), patch.object(bot, "reply", new_callable=AsyncMock):
                with self.assertRaises(RuntimeError):
                    await bot.handle_command(self.ctx, command)
                self.assertEqual(bot._active_resource_request_keys, set())

    def test_cloud_admin_panel_counts_come_from_control_api(self):
        client = Mock()
        client.counts.return_value = {"admins": 4, "staff": 1, "banned": 5}
        backend = CloudStateBackend(client)
        backend.login_staff("someone")
        self.assertEqual(
            backend.counts(), {"admins": 4, "staff": 1, "staff_online": 1, "banned": 5}
        )

    def test_control_service_counts_roles_and_bans(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine = create_app_engine(f"sqlite:///{tmp}/control.db")
            init_db(engine)
            service = ControlService(engine)
            service.add_role("a1", "admin")
            service.add_role("a2", "admin")
            service.add_role("s1", "staff")
            service.set_banned("b1", True)
            service.upsert_seen_user("u1")
            self.assertEqual(service.counts(), {"admins": 2, "staff": 1, "banned": 1})
            engine.dispose()


class SpeedTests(unittest.IsolatedAsyncioTestCase):
    async def test_email_sending_does_not_block_the_event_loop(self):
        def slow_send(*args, **kwargs):
            time.sleep(0.3)
            return True, ""

        ticks = 0

        async def ticker():
            nonlocal ticks
            for _ in range(5):
                await asyncio.sleep(0.02)
                ticks += 1

        with patch.object(email_sender, "send_email_sync", side_effect=slow_send):
            result, _ = await asyncio.gather(
                email_sender.send_email("h", 465, "u", "p", "f@x.com", "t@x.com", "s", "b"),
                ticker(),
            )
        self.assertEqual(result, (True, ""))
        self.assertEqual(ticks, 5)

    async def test_stock_queries_run_concurrently(self):
        async def slow(value):
            await asyncio.sleep(0.2)
            return value

        ctx = {"type": "c2c", "user_openid": "u", "msg_id": "m", "limit_user_id": "u"}
        with patch.object(bot.nfa, "get_nfa_stock", new=lambda: slow((True, 3, ""))), patch.object(
            bot.sauth, "get_4399_stock", new=lambda: slow((True, 5, 9, ""))
        ), patch.object(
            bot.sauth, "get_163_stock", new=lambda: slow((True, 1, 2, 1, ""))
        ), patch.object(bot, "reply", new_callable=AsyncMock) as reply:
            start = time.monotonic()
            await bot.handle_command(ctx, "/stock")
            elapsed = time.monotonic() - start
        self.assertLess(elapsed, 0.45)
        text = reply.await_args.args[1]
        self.assertIn("NFA：3", text)
        self.assertIn("4399：5/9", text)
        self.assertIn("163：1/2", text)

    async def test_stock_failure_of_one_source_keeps_the_others(self):
        ctx = {"type": "c2c", "user_openid": "u", "msg_id": "m", "limit_user_id": "u"}
        with patch.object(bot.nfa, "get_nfa_stock", new_callable=AsyncMock, side_effect=RuntimeError("x")), patch.object(
            bot.sauth, "get_4399_stock", new_callable=AsyncMock, return_value=(True, 5, 9, "")
        ), patch.object(
            bot.sauth, "get_163_stock", new_callable=AsyncMock, return_value=(False, 0, 0, 0, "e")
        ), patch.object(bot, "reply", new_callable=AsyncMock) as reply:
            await bot.handle_command(ctx, "/stock")
        text = reply.await_args.args[1]
        self.assertIn("NFA：unavailable", text)
        self.assertIn("4399：5/9", text)
        self.assertIn("163：unavailable", text)


class JsonStateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "sign.json")

    def tearDown(self):
        json_state._unreadable.discard(os.path.abspath(self.path))
        self.tmp.cleanup()

    def test_write_is_atomic_and_leaves_no_temp_files(self):
        self.assertTrue(json_state.write_json(self.path, {"a": {"points": 1}}))
        self.assertTrue(json_state.write_json(self.path, {"a": {"points": 2}}))
        self.assertEqual(json_state.read_json(self.path, {}), {"a": {"points": 2}})
        self.assertEqual(os.listdir(self.tmp.name), ["sign.json"])

    def test_corrupt_file_is_not_treated_as_empty_and_not_overwritten(self):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write('{"a": {"points": 50}, "b"')
        with self.assertLogs("QQBot", level="ERROR"):
            self.assertEqual(json_state.read_json(self.path, {}), {})
        self.assertFalse(json_state.write_json(self.path, {}))
        with open(self.path, encoding="utf-8") as f:
            self.assertEqual(f.read(), '{"a": {"points": 50}, "b"')
        self.assertTrue(any(name.startswith("sign.json.corrupt-") for name in os.listdir(self.tmp.name)))

    def test_block_lifts_after_the_file_is_fixed(self):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("{")
        with self.assertLogs("QQBot", level="ERROR"):
            json_state.read_json(self.path, {})
        with open(self.path, "w", encoding="utf-8") as f:
            f.write('{"ok": 1}')
        self.assertEqual(json_state.read_json(self.path, {}), {"ok": 1})
        self.assertTrue(json_state.write_json(self.path, {"ok": 2}))

    def test_bot_sign_data_survives_a_corrupt_file(self):
        old_file = bot.SIGN_FILE
        bot.SIGN_FILE = self.path
        try:
            with open(self.path, "w", encoding="utf-8") as f:
                f.write('{"user": {"points": 999')
            with self.assertLogs("QQBot", level="ERROR"):
                data = bot._load_sign()
            self.assertEqual(data, {})
            bot._save_sign({})
            with open(self.path, encoding="utf-8") as f:
                self.assertEqual(f.read(), '{"user": {"points": 999')
        finally:
            bot.SIGN_FILE = old_file


if __name__ == "__main__":
    unittest.main()
