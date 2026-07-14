import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import whatsapp


PHONE = "5511949662222"
PHONE_JID = f"{PHONE}@s.whatsapp.net"
LID = "280092081172570"
LID_JID = f"{LID}@lid"


class ChatIdentityTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.messages_db = os.path.join(self.temp_dir.name, "messages.db")
        self.whatsapp_db = os.path.join(self.temp_dir.name, "whatsapp.db")

        messages = sqlite3.connect(self.messages_db)
        messages.executescript("""
            CREATE TABLE chats (
                jid TEXT PRIMARY KEY,
                name TEXT,
                last_message_time TIMESTAMP,
                unread_count INTEGER DEFAULT 0
            );
            CREATE TABLE messages (
                id TEXT,
                chat_jid TEXT,
                sender TEXT,
                content TEXT,
                timestamp TIMESTAMP,
                is_from_me BOOLEAN,
                media_type TEXT,
                PRIMARY KEY (id, chat_jid)
            );
        """)
        messages.executemany(
            "INSERT INTO chats (jid, name, last_message_time, unread_count) VALUES (?, ?, ?, ?)",
            [
                (PHONE_JID, "8 Ball Shop", "2026-07-14T18:59:32-03:00", 0),
                (LID_JID, "8 Ball Shop LTDA", "2026-07-14T19:00:27-03:00", 4),
            ],
        )
        messages.executemany(
            """INSERT INTO messages
               (id, chat_jid, sender, content, timestamp, is_from_me, media_type)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            [
                ("sent-1", PHONE_JID, "me", "tem menthol v250?", "2026-07-14T18:59:32-03:00", 1, ""),
                ("reply-1", LID_JID, LID, "Conseguimos sim", "2026-07-14T19:00:18-03:00", 0, ""),
                ("reply-2", LID_JID, LID, "A entrega fica 15, total 175", "2026-07-14T19:00:27-03:00", 0, ""),
            ],
        )
        messages.commit()
        messages.close()

        store = sqlite3.connect(self.whatsapp_db)
        store.execute("CREATE TABLE whatsmeow_lid_map (lid TEXT PRIMARY KEY, pn TEXT UNIQUE)")
        store.execute("INSERT INTO whatsmeow_lid_map (lid, pn) VALUES (?, ?)", (LID, PHONE))
        store.execute("""
            CREATE TABLE whatsmeow_contacts (
                our_jid TEXT,
                their_jid TEXT,
                first_name TEXT,
                full_name TEXT,
                push_name TEXT,
                business_name TEXT,
                redacted_phone TEXT
            )
        """)
        store.execute(
            """INSERT INTO whatsmeow_contacts
               (our_jid, their_jid, first_name, full_name, push_name, business_name)
               VALUES (?, ?, ?, ?, ?, ?)""",
            ("me@s.whatsapp.net", LID_JID, "", "", "8 Ball Shop", "8 Ball Shop"),
        )
        store.commit()
        store.close()

        self.old_messages_path = whatsapp.MESSAGES_DB_PATH
        self.old_whatsapp_path = whatsapp.WHATSAPP_DB_PATH
        whatsapp.MESSAGES_DB_PATH = self.messages_db
        whatsapp.WHATSAPP_DB_PATH = self.whatsapp_db

    def tearDown(self):
        whatsapp.MESSAGES_DB_PATH = self.old_messages_path
        whatsapp.WHATSAPP_DB_PATH = self.old_whatsapp_path
        self.temp_dir.cleanup()

    def test_resolves_phone_and_lid_to_same_identity(self):
        from_phone = whatsapp.resolve_chat_identity(PHONE)
        from_lid = whatsapp.resolve_chat_identity(LID_JID)

        self.assertEqual(from_phone, from_lid)
        self.assertEqual(from_phone.canonical_jid, PHONE_JID)
        self.assertEqual(from_phone.aliases, (PHONE_JID, LID_JID))

    def test_list_messages_by_phone_includes_lid_replies(self):
        output = whatsapp.list_messages(
            chat_jid=PHONE,
            include_context=False,
            limit=10,
        )

        self.assertIn("Conseguimos sim", output)
        self.assertIn("A entrega fica 15, total 175", output)
        self.assertIn("From: 8 Ball Shop", output)

    def test_sender_filter_expands_phone_to_lid_sender(self):
        output = whatsapp.list_messages(
            sender_phone_number=PHONE,
            include_context=False,
            limit=10,
        )

        self.assertIn("Conseguimos sim", output)
        self.assertNotIn("tem menthol", output)

    def test_direct_chat_uses_most_recent_alias(self):
        chat = whatsapp.get_direct_chat_by_contact(PHONE)

        self.assertEqual(chat.jid, LID_JID)
        self.assertEqual(chat.last_message, "A entrega fica 15, total 175")

    def test_last_interaction_searches_all_aliases(self):
        output = whatsapp.get_last_interaction(PHONE_JID)

        self.assertIn("A entrega fica 15, total 175", output)

    def test_context_crosses_phone_and_lid_aliases(self):
        context = whatsapp.get_message_context("sent-1", after=5, chat_jid=PHONE_JID)

        self.assertEqual([message.id for message in context.after], ["reply-1", "reply-2"])

    def test_contact_search_returns_one_canonical_contact(self):
        contacts = whatsapp.search_contacts(PHONE)

        self.assertEqual(len(contacts), 1)
        self.assertEqual(contacts[0].phone_number, PHONE)
        self.assertEqual(contacts[0].jid, PHONE_JID)


if __name__ == "__main__":
    unittest.main()
