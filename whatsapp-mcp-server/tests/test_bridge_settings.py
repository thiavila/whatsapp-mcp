import os
import sys
import tempfile
import unittest
from unittest.mock import patch


SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if SERVER_DIR not in sys.path:
    sys.path.insert(0, SERVER_DIR)

import whatsapp


TOKEN = "test-token-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"


class BridgeSettingsTests(unittest.TestCase):
    """BR-3: bridge location comes from env, with the legacy values as defaults."""

    def test_defaults_match_legacy_paths(self):
        settings = whatsapp.load_bridge_settings({})
        self.assertEqual(settings["api_base_url"], "http://localhost:8080/api")
        store = os.path.join(whatsapp.BRIDGE_DIR, "store")
        self.assertEqual(settings["messages_db_path"], os.path.join(store, "messages.db"))
        self.assertEqual(settings["whatsapp_db_path"], os.path.join(store, "whatsapp.db"))

    def test_store_dir_and_port_derive_paths_and_url(self):
        settings = whatsapp.load_bridge_settings(
            {"WHATSAPP_STORE_DIR": "/srv/wa/comercial/store", "WHATSAPP_BRIDGE_PORT": "8081"}
        )
        self.assertEqual(settings["api_base_url"], "http://localhost:8081/api")
        self.assertEqual(settings["messages_db_path"], "/srv/wa/comercial/store/messages.db")
        self.assertEqual(settings["whatsapp_db_path"], "/srv/wa/comercial/store/whatsapp.db")

    def test_relative_store_dir_is_resolved_against_bridge_dir(self):
        settings = whatsapp.load_bridge_settings({"WHATSAPP_STORE_DIR": "store-ops"})
        self.assertEqual(
            settings["messages_db_path"],
            os.path.join(whatsapp.BRIDGE_DIR, "store-ops", "messages.db"),
        )

    def test_explicit_values_win(self):
        settings = whatsapp.load_bridge_settings(
            {
                "WHATSAPP_STORE_DIR": "/ignored",
                "WHATSAPP_BRIDGE_PORT": "9999",
                "WHATSAPP_API_BASE_URL": "http://127.0.0.1:8082/api/",
                "WHATSAPP_MESSAGES_DB_PATH": "/data/m.db",
                "WHATSAPP_DB_PATH": "/data/w.db",
            }
        )
        self.assertEqual(settings["api_base_url"], "http://127.0.0.1:8082/api")
        self.assertEqual(settings["messages_db_path"], "/data/m.db")
        self.assertEqual(settings["whatsapp_db_path"], "/data/w.db")


class BridgeTokenTests(unittest.TestCase):
    """BR-8: the MCP server sends the same bearer token the bridge requires."""

    def test_no_token_sends_no_header(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(whatsapp.bridge_headers(), {})

    def test_token_from_env(self):
        with patch.dict(os.environ, {"WHATSAPP_BRIDGE_TOKEN": f"  {TOKEN}\n"}, clear=True):
            self.assertEqual(whatsapp.bridge_headers(), {"Authorization": f"Bearer {TOKEN}"})

    def test_token_from_file(self):
        with tempfile.NamedTemporaryFile("w", delete=False) as handle:
            handle.write(TOKEN + "\n")
            path = handle.name
        try:
            with patch.dict(os.environ, {"WHATSAPP_BRIDGE_TOKEN_FILE": path}, clear=True):
                self.assertEqual(whatsapp.bridge_token(), TOKEN)
            with patch.dict(os.environ, {"WHATSAPP_BRIDGE_TOKEN_FILE": path + ".missing"}, clear=True):
                self.assertIsNone(whatsapp.bridge_token())
        finally:
            os.unlink(path)

    @patch("whatsapp.requests.post")
    def test_every_post_carries_the_token(self, post):
        post.return_value.status_code = 200
        post.return_value.json.return_value = {"success": True, "message": "ok"}
        with patch.dict(os.environ, {"WHATSAPP_BRIDGE_TOKEN": TOKEN}, clear=True):
            whatsapp.mark_messages_read("120363000000000001@g.us", ["ID1"])
            whatsapp.send_message("5500000000001", "oi", show_typing=False)
        self.assertEqual(post.call_count, 2)
        for call in post.call_args_list:
            self.assertEqual(call.kwargs["headers"], {"Authorization": f"Bearer {TOKEN}"})

    @patch("whatsapp.requests.get")
    def test_every_get_carries_the_token(self, get):
        get.return_value.status_code = 200
        get.return_value.json.return_value = {"success": True, "groups": []}
        with patch.dict(os.environ, {"WHATSAPP_BRIDGE_TOKEN": TOKEN}, clear=True):
            whatsapp.list_joined_groups()
        self.assertEqual(get.call_args.kwargs["headers"], {"Authorization": f"Bearer {TOKEN}"})

    @patch("whatsapp.requests.post")
    def test_unauthorized_response_explains_the_token(self, post):
        post.return_value.status_code = 401
        post.return_value.text = '{"success":false}'
        ok, message = whatsapp.send_message("5500000000001", "oi", show_typing=False)
        self.assertFalse(ok)
        self.assertIn("WHATSAPP_BRIDGE_TOKEN", message)
        self.assertNotIn(TOKEN, message)


class SendResultTests(unittest.TestCase):
    """BR-7: the send result exposes the WhatsApp message id."""

    @patch("whatsapp.requests.post")
    def test_send_message_detailed_returns_message_id(self, post):
        post.return_value.status_code = 200
        post.return_value.json.return_value = {
            "success": True,
            "message": "Message sent",
            "message_id": "3EB0AAAAAAAAAAAA",
            "timestamp": "2026-09-27T12:00:00Z",
        }
        result = whatsapp.send_message_detailed("5500000000001", "oi", show_typing=False)
        self.assertEqual(
            result,
            {
                "success": True,
                "message": "Message sent",
                "message_id": "3EB0AAAAAAAAAAAA",
                "timestamp": "2026-09-27T12:00:00Z",
            },
        )
        # The tuple API is unchanged for existing callers.
        self.assertEqual(
            whatsapp.send_message("5500000000001", "oi", show_typing=False),
            (True, "Message sent"),
        )

    @patch("whatsapp.requests.post")
    def test_old_bridge_without_message_id_still_works(self, post):
        post.return_value.status_code = 200
        post.return_value.json.return_value = {"success": True, "message": "sent"}
        result = whatsapp.send_message_detailed("5500000000001", "oi", show_typing=False)
        self.assertEqual(result, {"success": True, "message": "sent"})


if __name__ == "__main__":
    unittest.main()
