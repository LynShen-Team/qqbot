"""发号日志不要存账号密码：发送日志、命令日志、journal 预览、NFA SSE 日志、旧日志脱敏。"""

import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import bot
import redact
from adapters.qq_official import C2C_MESSAGE_CREATE
from control_api import redact_logs
from control_api.db import create_app_engine, init_db, session_scope
from control_api.models import OutboundLog
from control_api.service import ControlService

BODY_4399 = "主人您的4399账号来了喵~\n━━━━━━━━━━━━━━\n账号：user123\n密码：pa55word\n━━━━━━━━━━━━━━\n爱来自Miracle小号网站"
BODY_163 = "主人您的163小号来了喵~\n━━━━━━━━━━━━━━\n账号：a@163.com\n密码：x163pw\n━━━━━━━━━━━━━━"
BODY_SAUTH = '4399 Sauth：{"sauth_json":"abc-secret-token"}'
BODY_NFA = "主人您的nfa来了喵~\n━━━━━━━━━━━━━━\nID：nfauser\nUUID：1234\nToken：tok-secret"


class _Backend:
    def __init__(self):
        self.outbound, self.commands = [], []

    def log_outbound(self, **fields):
        self.outbound.append(fields)

    def log_command(self, **fields):
        self.commands.append(fields)


class RedactFunctionTests(unittest.TestCase):
    def test_resource_messages_become_type_only(self):
        for body, label in ((BODY_4399, "4399 账号"), (BODY_163, "163 小号"), (BODY_SAUTH, "4399 Sauth"), (BODY_NFA, "NFA")):
            with self.subTest(label=label):
                masked = redact.redact_outbound(body + "\n\n已消耗积分：2\n剩余积分：48")
                self.assertEqual(masked, f"[发号] {label}（已脱敏：账号密码不记录）")
                self.assertFalse(redact.contains_secret(masked))

    def test_other_messages_are_unchanged(self):
        for text in ("签到成功喵~\n获得 50 积分", "4399 账号 已发送到邮箱 12\\*\\*\\*3@qq.com，请查收喵~", ""):
            self.assertEqual(redact.redact_outbound(text), text)

    def test_unknown_message_with_password_is_masked_whole(self):
        self.assertEqual(redact.redact_outbound("你的号\n账号：a\n密码：b"), "[已脱敏] 消息含账号密码，正文不记录")

    def test_preview_masks_values_and_auth(self):
        self.assertEqual(redact.redact_preview("账号：abc 密码：def"), "账号：*** 密码：***")
        self.assertEqual(redact.redact_preview("/auth s3cret"), "/auth ***")
        self.assertEqual(redact.redact_preview("auth s3cret"), "auth ***")
        self.assertEqual(redact.redact_preview("/4399"), "/4399")
        self.assertEqual(redact.redact_preview("password: hunter2"), "password: ***")


class BotLoggingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.backend = _Backend()
        self.ctx = {"type": "c2c", "user_openid": "u1", "msg_id": "m1", "limit_user_id": "u1"}

    async def test_private_resource_delivery_logs_type_not_credentials(self):
        with patch.object(bot, "_state_backend", self.backend), patch.object(
            bot, "_get_active_ads", return_value=[]
        ), patch.object(bot, "_get_bound_email", return_value=""), patch.object(
            bot, "_send_text", new_callable=AsyncMock, return_value=True
        ) as send:
            ok = await bot._send_resource_result(self.ctx, "u1", "4399", "4399 账号", "s", BODY_4399, quota_text="剩余积分：48")
        self.assertTrue(ok)
        self.assertIn("pa55word", send.await_args.args[2])  # the user still gets the account
        logged = self.backend.outbound[0]
        self.assertEqual(logged["content"], "[发号] 4399 账号（已脱敏：账号密码不记录）")
        self.assertEqual(logged["status"], "success")
        self.assertNotIn("pa55word", str(self.backend.outbound))

    async def test_failed_delivery_is_logged_as_failed_without_credentials(self):
        with patch.object(bot, "_state_backend", self.backend), patch.object(
            bot, "_send_text", new_callable=AsyncMock, return_value=False
        ):
            await bot.reply(self.ctx, BODY_163)
        self.assertEqual(self.backend.outbound[0]["status"], "failed")
        self.assertNotIn("x163pw", str(self.backend.outbound))

    async def test_auth_passphrase_is_not_written_to_the_command_log(self):
        with patch.object(bot, "_state_backend", self.backend):
            bot._log_command_event(self.ctx, "/auth topsecret")
        self.assertEqual(self.backend.commands[0]["content"], "/auth ***")
        self.assertEqual(self.backend.commands[0]["command"], "/auth")

    async def test_incoming_message_preview_in_journal_is_masked(self):
        data = {"id": "m9", "content": "账号：leak1\n密码：leak2", "author": {"user_openid": "u9"}}
        with patch.object(bot, "process_message", new_callable=AsyncMock), self.assertLogs("OfficialBot", level="INFO") as logs:
            await bot.handle_c2c_message(data)
        text = "\n".join(logs.output)
        self.assertIn("[私聊]", text)
        self.assertNotIn("leak1", text)
        self.assertNotIn("leak2", text)


class NfaSseLoggingTests(unittest.TestCase):
    def test_nfa_data_lines_are_not_logged(self):
        source = open(bot.nfa.__file__, encoding="utf-8").read()
        self.assertNotIn('_log.info(f"[NFA] 第{line_count}行: {line[:200]}")', source)
        self.assertIn("内容不记录", source)


class ExistingLogRedactionTests(unittest.TestCase):
    def test_redacts_only_credential_rows_and_keeps_everything_else(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine = create_app_engine(f"sqlite:///{tmp}/control.db")
            init_db(engine)
            with session_scope(engine) as s:
                s.add_all([
                    OutboundLog(user_key="a", channel="c2c", status="success", content=BODY_4399, message_id="1"),
                    OutboundLog(user_key="b", channel="c2c", status="failed", content=BODY_4399, message_id="2"),
                    OutboundLog(user_key="c", channel="c2c", status="success", content=BODY_SAUTH, message_id="3"),
                    OutboundLog(user_key="d", channel="c2c", status="success", content=BODY_163, message_id="4"),
                    OutboundLog(user_key="e", channel="group", status="success", content="签到成功喵~", message_id="5"),
                    OutboundLog(user_key="f", channel="email", status="success", content="Miracle 4399 账号密码 -> 12***3@qq.com", message_id="6"),
                ])
            dry = redact_logs.redact_outbound_logs(engine, dry_run=True)
            self.assertEqual((dry["matched"], dry["updated"]), (4, 0))
            result = redact_logs.redact_outbound_logs(engine)
            self.assertEqual(result["matched"], 4)
            self.assertEqual(result["updated"], 4)
            self.assertEqual(result["remaining_with_secrets"], 0)
            self.assertEqual(result["by_type"], {"4399 账号": 2, "4399 Sauth": 1, "163 小号": 1})
            rows = {r["message_id"]: r for r in ControlService(engine).list_outbound_logs()}
            self.assertEqual(len(rows), 6)
            self.assertEqual(rows["2"]["content"], "[发号] 4399 账号（已脱敏：账号密码不记录）")
            self.assertEqual(rows["2"]["status"], "failed")
            self.assertEqual(rows["5"]["content"], "签到成功喵~")
            self.assertEqual(rows["6"]["content"], "Miracle 4399 账号密码 -> 12***3@qq.com")
            again = redact_logs.redact_outbound_logs(engine)
            self.assertEqual(again["matched"], 0)
            engine.dispose()

    def test_control_api_masks_even_if_a_client_sends_credentials(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine = create_app_engine(f"sqlite:///{tmp}/control.db")
            init_db(engine)
            service = ControlService(engine)
            service.log_outbound(user_key="a", channel="c2c", status="success", content=BODY_NFA)
            service.log_command(user_key="a", command="/auth", content="/auth pw")
            self.assertEqual(service.list_outbound_logs()[0]["content"], "[发号] NFA（已脱敏：账号密码不记录）")
            self.assertEqual(service.list_command_logs()[0]["content"], "/auth ***")
            engine.dispose()


if __name__ == "__main__":
    unittest.main()
