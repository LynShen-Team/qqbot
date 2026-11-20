import unittest
from unittest.mock import patch

import bot


class _Resp:
    status_code = 200
    text = "ok"

    def json(self):
        return {}


class _CountingClient:
    created = 0

    def __init__(self, **kwargs):
        type(self).created += 1
        self.kwargs = kwargs
        self.is_closed = False
        self.posts = 0

    async def post(self, *args, **kwargs):
        self.posts += 1
        return _Resp()


class SharedQQHttpTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        _CountingClient.created = 0
        bot._qq_http_clients.clear()
        self.old_token, self.old_expire = bot._access_token, bot._token_expire_at
        bot._access_token, bot._token_expire_at = "token", 9999999999

    def tearDown(self):
        bot._qq_http_clients.clear()
        bot._access_token, bot._token_expire_at = self.old_token, self.old_expire

    async def test_one_client_per_timeout_and_kept_open(self):
        with patch.object(bot.httpx, "AsyncClient", _CountingClient):
            async with bot._shared_qq_http(15) as a:
                pass
            async with bot._shared_qq_http(15) as b:
                pass
            async with bot._shared_qq_http(60) as c:
                pass
        self.assertIs(a, b)
        self.assertIsNot(a, c)
        self.assertEqual(_CountingClient.created, 2)
        self.assertFalse(a.is_closed)
        self.assertEqual(a.kwargs["timeout"], 15)
        self.assertEqual(a.kwargs["limits"].keepalive_expiry, 60)

    async def test_consecutive_sends_reuse_the_same_client(self):
        with patch.object(bot.httpx, "AsyncClient", _CountingClient):
            self.assertTrue(await bot.send_group_msg("g1", "hello", "m1", keyboard={}))
            self.assertTrue(await bot.send_c2c_msg("u1", "hello", "m2", keyboard={}))
        self.assertEqual(_CountingClient.created, 1)

    async def test_closed_client_is_replaced(self):
        with patch.object(bot.httpx, "AsyncClient", _CountingClient):
            async with bot._shared_qq_http(15) as a:
                a.is_closed = True
            async with bot._shared_qq_http(15) as b:
                pass
        self.assertIsNot(a, b)
        self.assertEqual(_CountingClient.created, 2)


if __name__ == "__main__":
    unittest.main()
