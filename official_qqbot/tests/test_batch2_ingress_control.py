"""Batch 2: callbacks acked before processing, control calls off the hot path, dashboard paging, log retention."""

import asyncio
import json
import tempfile
import unittest
from datetime import timedelta
from unittest.mock import AsyncMock, Mock, patch

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import bot
from control_api.client import AsyncControlApiClient
from control_api.db import create_app_engine, init_db
from control_api.models import CommandLog, OutboundLog, utc_now
from control_api.db import session_scope
from control_api.service import ControlService
from state_backend import CloudStateBackend, ControlEventQueue


class _SignedWebhookBase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        for name, value in (("APP_ID", "app"), ("APP_SECRET", "test-secret")):
            mock = patch.object(bot, name, value)
            mock.start()
            self.addCleanup(mock.stop)
        app = web.Application()
        app.router.add_post("/qq", bot._receive_qq_webhook)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await bot.drain_inbound_tasks(timeout=2)
        await self.client.close()

    def signed(self, payload):
        body = json.dumps(payload, ensure_ascii=False).encode()
        timestamp = "1791129600"
        headers = {
            "Content-Type": "application/json",
            "X-Bot-Appid": "app",
            "X-Signature-Timestamp": timestamp,
            "X-Signature-Ed25519": bot._qq_webhook_key("test-secret").sign(timestamp.encode() + body).hex(),
        }
        return body, headers


class AsyncAckTests(_SignedWebhookBase):
    async def test_dispatch_is_acknowledged_before_processing_finishes(self):
        release = asyncio.Event()
        started = asyncio.Event()

        async def slow_handler(data, event_type):
            started.set()
            await release.wait()

        body, headers = self.signed({"op": 0, "t": "GROUP_AT_MESSAGE_CREATE", "d": {"id": "m1", "content": "/help"}})
        with patch.object(bot, "QQ_WEBHOOK_ASYNC", True), patch.object(bot, "handle_group_message", side_effect=slow_handler):
            response = await asyncio.wait_for(self.client.post("/qq", data=body, headers=headers), 2)
            self.assertEqual(response.status, 200)
            self.assertEqual(await response.json(), {"status": "success"})
            await asyncio.wait_for(started.wait(), 2)
            self.assertEqual(len(bot._inbound_tasks), 1)
            release.set()
            await bot.drain_inbound_tasks(timeout=2)
        self.assertEqual(len(bot._inbound_tasks), 0)

    async def test_switch_off_restores_answer_after_processing(self):
        done = []

        async def handler(data, event_type):
            await asyncio.sleep(0.05)
            done.append(event_type)

        body, headers = self.signed({"op": 0, "t": "GROUP_AT_MESSAGE_CREATE", "d": {"id": "m2"}})
        with patch.object(bot, "QQ_WEBHOOK_ASYNC", False), patch.object(bot, "handle_group_message", side_effect=handler):
            response = await self.client.post("/qq", data=body, headers=headers)
            self.assertEqual(done, ["GROUP_AT_MESSAGE_CREATE"])
        self.assertEqual(response.status, 200)
        self.assertEqual(len(bot._inbound_tasks), 0)

    async def test_url_verification_is_still_answered_synchronously(self):
        with patch.object(bot, "QQ_WEBHOOK_ASYNC", True):
            response = await self.client.post(
                "/qq", json={"op": 13, "d": {"event_ts": "123", "plain_token": "plain"}}, headers={"X-Bot-Appid": "app"}
            )
        payload = await response.json()
        self.assertEqual(payload["op"], 12)
        self.assertEqual(payload["d"]["random_key"], "plain")

    async def test_background_failure_is_logged_not_lost(self):
        body, headers = self.signed({"op": 0, "t": "C2C_MESSAGE_CREATE", "d": {"id": "m3"}})
        with patch.object(bot, "QQ_WEBHOOK_ASYNC", True), patch.object(
            bot, "handle_c2c_message", new_callable=AsyncMock, side_effect=RuntimeError("boom")
        ), self.assertLogs("OfficialBot", level="ERROR") as logs:
            response = await self.client.post("/qq", data=body, headers=headers)
            await bot.drain_inbound_tasks(timeout=2)
        self.assertEqual(response.status, 200)
        self.assertIn("C2C_MESSAGE_CREATE 处理失败", "\n".join(logs.output))

    async def test_bad_signature_is_still_rejected_and_not_processed(self):
        body, headers = self.signed({"op": 0, "t": "GROUP_AT_MESSAGE_CREATE", "d": {}})
        with patch.object(bot, "QQ_WEBHOOK_ASYNC", True), patch.object(
            bot, "handle_group_message", new_callable=AsyncMock
        ) as handler:
            response = await self.client.post("/qq", data=body + b" ", headers=headers)
            await bot.drain_inbound_tasks(timeout=2)
        self.assertEqual(response.status, 403)
        handler.assert_not_awaited()


class DispatchTests(unittest.IsolatedAsyncioTestCase):
    async def test_ws_and_webhook_share_one_dispatcher(self):
        for event_type, attr in (
            ("GROUP_AT_MESSAGE_CREATE", "handle_group_message"),
            ("C2C_MESSAGE_CREATE", "handle_c2c_message"),
            ("GROUP_ADD_ROBOT", "handle_group_add_robot"),
        ):
            with self.subTest(event_type=event_type), patch.object(bot, attr, new_callable=AsyncMock) as handler:
                await bot.dispatch_qq_event(event_type, {"id": "x"}, source="ws")
                handler.assert_awaited_once()

    async def test_spawned_tasks_are_kept_until_done(self):
        gate = asyncio.Event()

        async def work():
            await gate.wait()

        task = bot._spawn_inbound(work(), "TEST")
        self.assertIn(task, bot._inbound_tasks)
        gate.set()
        await bot.drain_inbound_tasks(timeout=1)
        self.assertNotIn(task, bot._inbound_tasks)

    async def test_graceful_shutdown_waits_for_in_flight_events(self):
        finished = []

        async def work():
            await asyncio.sleep(0.05)
            finished.append(True)

        async def forever():
            await asyncio.Event().wait()

        bot._spawn_inbound(work(), "TEST")
        runtime = asyncio.gather(forever())
        await bot._graceful_shutdown(runtime)
        self.assertEqual(finished, [True])
        self.assertTrue(runtime.done())


class _FakeAsyncClient:
    def __init__(self):
        self.calls = []
        self.state = {"is_banned": False, "email": "a@example.com", "roles": ["staff"], "is_admin": False}

    async def get_user_state(self, user_key):
        self.calls.append(("get_user_state", user_key))
        return dict(self.state)

    async def log_command(self, **fields):
        self.calls.append(("log_command", fields))

    async def log_outbound(self, **fields):
        self.calls.append(("log_outbound", fields))

    async def seen_user(self, **fields):
        self.calls.append(("seen_user", fields))


class CloudStateTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.sync_client = Mock()
        self.sync_client.get_user_state.return_value = {"is_banned": True}
        self.async_client = _FakeAsyncClient()
        self.backend = CloudStateBackend(self.sync_client, self.async_client)

    async def test_user_state_is_fetched_once_per_message(self):
        token = await self.backend.prefetch_user_state("u1")
        try:
            self.assertFalse(self.backend.is_banned("u1"))
            self.assertEqual(self.backend.get_email_binding("u1"), "a@example.com")
            self.assertTrue(self.backend.staff_exists("u1"))
            self.assertFalse(self.backend.is_admin("u1"))
        finally:
            self.backend.end_message(token)
        self.assertEqual(self.async_client.calls, [("get_user_state", "u1")])
        self.sync_client.get_user_state.assert_not_called()
        # outside a message the old synchronous path is used
        self.assertTrue(self.backend.is_banned("u1"))
        self.sync_client.get_user_state.assert_called_once_with("u1")

    async def test_writes_invalidate_the_cached_state(self):
        token = await self.backend.prefetch_user_state("u1")
        try:
            self.backend.set_email_binding("u1", "new@example.com")
            self.sync_client.get_user_state.return_value = {"email": "new@example.com"}
            self.assertEqual(self.backend.get_email_binding("u1"), "new@example.com")
        finally:
            self.backend.end_message(token)

    async def test_logs_and_seen_go_through_the_background_queue(self):
        self.backend.log_command(user_key="u1", command="/help", content="/help")
        self.backend.log_outbound(user_key="u1", channel="c2c", status="success", content="hi")
        self.backend.seen_user("u1", group_openid="g1")
        self.sync_client.log_command.assert_not_called()
        self.sync_client.log_outbound.assert_not_called()
        self.sync_client.seen_user.assert_not_called()
        await self.backend.drain(timeout=1)
        self.assertEqual([c[0] for c in self.async_client.calls], ["log_command", "log_outbound", "seen_user"])

    def test_without_a_running_loop_logs_fall_back_to_the_sync_client(self):
        self.backend.log_command(user_key="u1", command="/help")
        self.sync_client.log_command.assert_called_once()

    async def test_full_queue_drops_instead_of_blocking(self):
        gate = asyncio.Event()

        class Stuck(_FakeAsyncClient):
            async def log_command(self, **fields):
                await gate.wait()

        queue = ControlEventQueue(Stuck(), maxsize=2)
        for _ in range(5):
            queue.submit("log_command", user_key="u")
        await asyncio.sleep(0)
        self.assertGreaterEqual(queue.dropped, 2)
        gate.set()
        await queue.drain(timeout=1)

    async def test_process_message_prefetches_state_once(self):
        backend = CloudStateBackend(Mock(), _FakeAsyncClient())
        ctx = {"type": "c2c", "user_openid": "u1", "msg_id": "m", "limit_user_id": "u1"}
        with patch.object(bot, "_state_backend", backend), patch.object(bot, "reply", new_callable=AsyncMock):
            await bot.process_message(ctx, "/help")
            await bot.process_message(ctx, "运势")
            await backend.drain(timeout=1)
        state_calls = [c for c in backend.async_client.calls if c[0] == "get_user_state"]
        self.assertEqual(len(state_calls), 2)
        backend.client.get_user_state.assert_not_called()


class AsyncControlClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_requests_carry_the_bot_token(self):
        import httpx

        seen = []

        def handler(request):
            seen.append((request.method, request.url.path, request.headers.get("X-Bot-Token")))
            return httpx.Response(200, json={"ok": True})

        client = AsyncControlApiClient("http://control", "tok", transport=httpx.MockTransport(handler))
        await client.get_user_state("u1")
        await client.log_command(user_key="u1", command="/x")
        self.assertEqual(seen, [("GET", "/internal/users/u1/state", "tok"), ("POST", "/internal/logs/command", "tok")])


class ControlServiceBatch2Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.engine = create_app_engine(f"sqlite:///{self.tmp.name}/control.db")
        init_db(self.engine)
        self.service = ControlService(self.engine)

    def tearDown(self):
        self.engine.dispose()
        self.tmp.cleanup()

    def test_connections_are_pooled(self):
        self.assertNotEqual(type(self.engine.pool).__name__, "NullPool")

    def test_user_page_joins_email_and_roles(self):
        for key in ("a", "b", "c"):
            self.service.upsert_seen_user(key)
        self.service.set_email_binding("b", "b@example.com")
        self.service.add_role("b", "admin")
        page = self.service.list_users_page(page=1, page_size=2)
        self.assertEqual((page["total"], page["pages"], len(page["items"])), (3, 2, 2))
        by_key = {item["user_key"]: item for item in page["items"]}
        self.assertEqual(by_key["b"]["email"], "b@example.com")
        self.assertEqual(by_key["b"]["roles"], ["admin"])
        self.assertEqual(self.service.list_users_page(page=99, page_size=2)["page"], 2)
        self.assertEqual(self.service.list_users_page(query="c")["total"], 1)

    def test_purge_logs_removes_only_old_rows_and_zero_means_keep(self):
        old = utc_now() - timedelta(days=40)
        with session_scope(self.engine) as session:
            session.add_all([
                CommandLog(user_key="u", command="old", created_at=old),
                CommandLog(user_key="u", command="new"),
                OutboundLog(user_key="u", content="old", created_at=old),
                OutboundLog(user_key="u", content="new"),
            ])
        self.assertEqual(self.service.purge_logs(0), {"command_logs": 0, "outbound_logs": 0})
        self.assertEqual(self.service.purge_logs(30, batch_size=1), {"command_logs": 1, "outbound_logs": 1})
        self.assertEqual([r["command"] for r in self.service.list_command_logs()], ["new"])
        self.assertEqual([r["content"] for r in self.service.list_outbound_logs()], ["new"])


if __name__ == "__main__":
    unittest.main()
