"""AI 回复里的 [---SPLIT---] 要拆成几条发，不能原样出现在消息里。"""

import unittest
from unittest.mock import AsyncMock, patch

import bot
import messages

REPLY = (
    "(耳朵抖了抖)\n晚上好喵\n[---SPLIT---]\n(把书合上)\n不过这个点还醒着\n"
    "[---SPLIT---]\n(凑近了一点)\n说吧说吧"
)


class SplitTests(unittest.TestCase):
    def test_split_marker_is_removed(self):
        parts = messages.split_ai_reply(REPLY)
        self.assertEqual(len(parts), 3)
        self.assertTrue(all("SPLIT" not in part for part in parts))
        self.assertEqual(parts[0], "(耳朵抖了抖)\n晚上好喵")

    def test_plain_reply_is_one_part(self):
        self.assertEqual(messages.split_ai_reply("就一句喵"), ["就一句喵"])

    def test_more_than_three_parts_are_merged_into_the_last(self):
        parts = messages.split_ai_reply("a[---SPLIT---]b[---SPLIT---]c[---SPLIT---]d")
        self.assertEqual(parts, ["a", "b", "c\n\nd"])

    def test_marker_variants(self):
        self.assertEqual(messages.split_ai_reply("a [--- split ---] b"), ["a", "b"])
        self.assertEqual(messages.split_ai_reply("a[--SPLIT--]b"), ["a", "b"])


class ReplyTests(unittest.IsolatedAsyncioTestCase):
    async def test_ai_chat_reply_is_sent_as_separate_messages(self):
        ctx = {"type": "c2c", "user_openid": "u1", "msg_id": "m1", "limit_user_id": "u1"}
        with patch.object(bot.ai_chat, "chat", new_callable=AsyncMock, return_value=REPLY), patch.object(
            bot, "reply", new_callable=AsyncMock
        ) as reply, patch.object(bot, "reply_plain", new_callable=AsyncMock) as reply_plain, patch.object(
            bot, "_record_seen_user"
        ):
            await bot.process_message(ctx, "晚上好呀")
        sent = [call.args[1] for call in reply_plain.await_args_list] + [call.args[1] for call in reply.await_args_list]
        self.assertEqual(len(sent), 3)
        self.assertTrue(all("SPLIT" not in text for text in sent))
        self.assertEqual(reply.await_count, 1)  # last part keeps the buttons
        self.assertIn("说吧说吧", reply.await_args.args[1])


if __name__ == "__main__":
    unittest.main()
