import os
import sys
import unittest
from unittest.mock import MagicMock, call, patch


SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if SERVER_DIR not in sys.path:
    sys.path.insert(0, SERVER_DIR)

import whatsapp


class SendTypingTests(unittest.TestCase):
    @patch("whatsapp.random.uniform", return_value=1.0)
    def test_typing_delay_scales_with_length_and_is_bounded(self, _uniform):
        self.assertEqual(whatsapp._typing_delay_seconds("short"), 1.0)
        self.assertEqual(whatsapp._typing_delay_seconds("x" * 60), 5.0)
        self.assertEqual(whatsapp._typing_delay_seconds("x" * 1000), 12.0)

    @patch("whatsapp.time.sleep")
    @patch("whatsapp.send_typing_indicator")
    @patch("whatsapp.resolve_chat_identity")
    @patch("whatsapp.requests.post")
    def test_send_message_types_waits_sends_and_clears(
        self, post, resolve_identity, typing, sleep
    ):
        resolve_identity.return_value = MagicMock(canonical_jid="5511999999999@s.whatsapp.net")
        typing.return_value = (True, "Presence sent")
        post.return_value.status_code = 200
        post.return_value.json.return_value = {"success": True, "message": "sent"}

        with patch("whatsapp._typing_delay_seconds", return_value=3.25):
            result = whatsapp.send_message("5511999999999", "hello")

        self.assertEqual(result, (True, "sent"))
        self.assertEqual(
            typing.call_args_list,
            [
                call("5511999999999@s.whatsapp.net", is_typing=True),
                call("5511999999999@s.whatsapp.net", is_typing=False),
            ],
        )
        sleep.assert_called_once_with(3.25)
        post.assert_called_once()

    @patch("whatsapp.time.sleep")
    @patch("whatsapp.send_typing_indicator")
    @patch("whatsapp.resolve_chat_identity")
    @patch("whatsapp.requests.post")
    def test_send_message_can_disable_typing(
        self, post, resolve_identity, typing, sleep
    ):
        post.return_value.status_code = 200
        post.return_value.json.return_value = {"success": True, "message": "sent"}

        result = whatsapp.send_message("5511999999999", "hello", show_typing=False)

        self.assertEqual(result, (True, "sent"))
        resolve_identity.assert_not_called()
        typing.assert_not_called()
        sleep.assert_not_called()

    @patch("whatsapp.time.sleep")
    @patch("whatsapp.send_typing_indicator")
    @patch("whatsapp.resolve_chat_identity")
    @patch("whatsapp.requests.post")
    def test_typing_is_cleared_when_send_fails(
        self, post, resolve_identity, typing, sleep
    ):
        resolve_identity.return_value = MagicMock(canonical_jid="5511999999999@s.whatsapp.net")
        typing.return_value = (True, "Presence sent")
        post.side_effect = whatsapp.requests.RequestException("offline")

        result = whatsapp.send_message("5511999999999", "hello")

        self.assertEqual(result, (False, "Request error: offline"))
        self.assertEqual(typing.call_count, 2)


if __name__ == "__main__":
    unittest.main()
