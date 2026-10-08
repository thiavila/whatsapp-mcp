package main

import (
	"database/sql"
	"encoding/json"
	"fmt"
	"time"

	"go.mau.fi/whatsmeow"
	waProto "go.mau.fi/whatsmeow/binary/proto"
	"go.mau.fi/whatsmeow/proto/waWeb"
	"go.mau.fi/whatsmeow/types"
)

// MessageMeta is the trusted, network-derived metadata stored next to each
// message (BR-4). Consumers such as the agent watchers use it to identify the
// sender reliably in groups, where WhatsApp increasingly uses LIDs.
type MessageMeta struct {
	// SenderJID is the full sender JID without device suffix, e.g.
	// "123456789012345@lid" or "5511999999999@s.whatsapp.net". The legacy
	// `sender` column keeps only the user part for backwards compatibility.
	SenderJID string
	// SenderAltJID is the alternative address (PN for a LID sender and
	// vice versa) when WhatsApp provides it.
	SenderAltJID string
	// PushName is the display name chosen by the sender. It is NOT verified.
	PushName        string
	MentionedJIDs   []string
	QuotedMessageID string
	IsForwarded     bool
}

// messageColumnMigrations lists the columns added to `messages` after the
// original schema. Each is added only when missing, so old databases upgrade
// in place on first start and repeated starts are no-ops.
var messageColumnMigrations = []struct{ table, column, ddl string }{
	{"chats", "unread_count", "ALTER TABLE chats ADD COLUMN unread_count INTEGER DEFAULT 0"},
	{"messages", "is_read", "ALTER TABLE messages ADD COLUMN is_read BOOLEAN DEFAULT FALSE"},
	{"messages", "sender_jid", "ALTER TABLE messages ADD COLUMN sender_jid TEXT"},
	{"messages", "sender_alt_jid", "ALTER TABLE messages ADD COLUMN sender_alt_jid TEXT"},
	{"messages", "push_name", "ALTER TABLE messages ADD COLUMN push_name TEXT"},
	{"messages", "mentioned_jids", "ALTER TABLE messages ADD COLUMN mentioned_jids TEXT"},
	{"messages", "quoted_message_id", "ALTER TABLE messages ADD COLUMN quoted_message_id TEXT"},
	{"messages", "is_forwarded", "ALTER TABLE messages ADD COLUMN is_forwarded BOOLEAN DEFAULT FALSE"},
	{"messages", "edited_at", "ALTER TABLE messages ADD COLUMN edited_at TIMESTAMP"},
	{"messages", "revoked_at", "ALTER TABLE messages ADD COLUMN revoked_at TIMESTAMP"},
}

// messageEventsSchema is the append-only log of edits and revocations
// (BR-5). The original row in `messages` keeps the content as first received.
const messageEventsSchema = `
	CREATE TABLE IF NOT EXISTS message_events (
		event_id TEXT NOT NULL,
		chat_jid TEXT NOT NULL,
		target_message_id TEXT NOT NULL,
		event_type TEXT NOT NULL,
		sender_jid TEXT,
		new_content TEXT,
		timestamp TIMESTAMP,
		is_from_me BOOLEAN DEFAULT FALSE,
		PRIMARY KEY (event_id, chat_jid)
	);
	CREATE INDEX IF NOT EXISTS idx_message_events_target ON message_events (chat_jid, target_message_id);
`

func tableHasColumn(db *sql.DB, table, column string) (bool, error) {
	rows, err := db.Query(fmt.Sprintf("PRAGMA table_info(%s)", table))
	if err != nil {
		return false, err
	}
	defer rows.Close()
	for rows.Next() {
		var cid int
		var name, ctype string
		var notnull, pk int
		var dflt sql.NullString
		if err := rows.Scan(&cid, &name, &ctype, &notnull, &dflt, &pk); err != nil {
			return false, err
		}
		if name == column {
			return true, nil
		}
	}
	return false, rows.Err()
}

// migrateMessageSchema upgrades databases created by older bridge versions.
func migrateMessageSchema(db *sql.DB) error {
	for _, m := range messageColumnMigrations {
		has, err := tableHasColumn(db, m.table, m.column)
		if err != nil {
			return err
		}
		if !has {
			if _, err := db.Exec(m.ddl); err != nil {
				return fmt.Errorf("add %s.%s: %v", m.table, m.column, err)
			}
		}
	}
	if _, err := db.Exec(messageEventsSchema); err != nil {
		return fmt.Errorf("create message_events: %v", err)
	}
	return nil
}

func messageContextInfo(msg *waProto.Message) *waProto.ContextInfo {
	if msg == nil {
		return nil
	}
	switch {
	case msg.GetExtendedTextMessage() != nil:
		return msg.GetExtendedTextMessage().GetContextInfo()
	case msg.GetImageMessage() != nil:
		return msg.GetImageMessage().GetContextInfo()
	case msg.GetVideoMessage() != nil:
		return msg.GetVideoMessage().GetContextInfo()
	case msg.GetAudioMessage() != nil:
		return msg.GetAudioMessage().GetContextInfo()
	case msg.GetDocumentMessage() != nil:
		return msg.GetDocumentMessage().GetContextInfo()
	case msg.GetStickerMessage() != nil:
		return msg.GetStickerMessage().GetContextInfo()
	case msg.GetContactMessage() != nil:
		return msg.GetContactMessage().GetContextInfo()
	case msg.GetLocationMessage() != nil:
		return msg.GetLocationMessage().GetContextInfo()
	}
	return nil
}

// extractMessageMeta builds the BR-4 metadata from a parsed message.
func extractMessageMeta(info types.MessageInfo, msg *waProto.Message) MessageMeta {
	meta := MessageMeta{PushName: info.PushName}
	if !info.Sender.IsEmpty() {
		meta.SenderJID = info.Sender.ToNonAD().String()
	}
	if !info.SenderAlt.IsEmpty() {
		meta.SenderAltJID = info.SenderAlt.ToNonAD().String()
	}
	if ctx := messageContextInfo(msg); ctx != nil {
		meta.MentionedJIDs = ctx.GetMentionedJID()
		meta.QuotedMessageID = ctx.GetStanzaID()
		meta.IsForwarded = ctx.GetIsForwarded()
	}
	return meta
}

// historyMessageMeta derives BR-4 metadata for a history-sync message. It
// prefers whatsmeow's own parser (which resolves LID/PN alternates) and falls
// back to the raw message key.
func historyMessageMeta(client *whatsmeow.Client, chat types.JID, webMsg *waWeb.WebMessageInfo) MessageMeta {
	if webMsg == nil {
		return MessageMeta{}
	}
	if client != nil && client.Store != nil && client.Store.ID != nil {
		if evt, err := client.ParseWebMessage(chat, webMsg); err == nil && evt != nil {
			return extractMessageMeta(evt.Info, evt.Message)
		}
	}
	meta := MessageMeta{PushName: webMsg.GetPushName()}
	key := webMsg.GetKey()
	switch {
	case key.GetFromMe():
		if client != nil && client.Store != nil && client.Store.ID != nil {
			meta.SenderJID = client.Store.ID.ToNonAD().String()
		}
	case key.GetParticipant() != "":
		if jid, err := types.ParseJID(key.GetParticipant()); err == nil {
			meta.SenderJID = jid.ToNonAD().String()
		}
	case chat.Server != types.GroupServer:
		meta.SenderJID = chat.ToNonAD().String()
	}
	if ctx := messageContextInfo(webMsg.GetMessage()); ctx != nil {
		meta.MentionedJIDs = ctx.GetMentionedJID()
		meta.QuotedMessageID = ctx.GetStanzaID()
		meta.IsForwarded = ctx.GetIsForwarded()
	}
	return meta
}

func nullableString(value string) any {
	if value == "" {
		return nil
	}
	return value
}

func (meta MessageMeta) mentionedJSON() any {
	if len(meta.MentionedJIDs) == 0 {
		return nil
	}
	data, err := json.Marshal(meta.MentionedJIDs)
	if err != nil {
		return nil
	}
	return string(data)
}

// MessageEvent is one edit or revocation observed for a stored message.
type MessageEvent struct {
	EventID         string
	ChatJID         string
	TargetMessageID string
	Type            string // "edit" or "revoke"
	SenderJID       string
	NewContent      string
	Timestamp       time.Time
	IsFromMe        bool
	// AliasChatJIDs are other JIDs verified (through the device's LID map) to
	// name the same 1:1 chat as ChatJID. Groups have none. The target row is
	// only ever looked up in ChatJID or one of these, never in other chats.
	AliasChatJIDs []string
}

const (
	messageEventEdit   = "edit"
	messageEventRevoke = "revoke"
)

// protocolMessageEvent recognises edits and revocations. whatsmeow unwraps
// EditedMessage, so both arrive as a ProtocolMessage pointing at the target.
func protocolMessageEvent(info types.MessageInfo, msg *waProto.Message) (MessageEvent, bool) {
	pm := msg.GetProtocolMessage()
	if pm == nil || pm.GetKey().GetID() == "" {
		return MessageEvent{}, false
	}
	event := MessageEvent{
		EventID:         info.ID,
		ChatJID:         info.Chat.String(),
		TargetMessageID: pm.GetKey().GetID(),
		Timestamp:       info.Timestamp,
		IsFromMe:        info.IsFromMe,
	}
	if !info.Sender.IsEmpty() {
		event.SenderJID = info.Sender.ToNonAD().String()
	}
	switch pm.GetType() {
	case waProto.ProtocolMessage_MESSAGE_EDIT:
		event.Type = messageEventEdit
		edited := pm.GetEditedMessage()
		event.NewContent = extractTextContent(edited)
		if event.NewContent == "" {
			event.NewContent = mediaCaption(edited)
		}
		if ms := pm.GetTimestampMS(); ms > 0 {
			event.Timestamp = time.UnixMilli(ms)
		}
	case waProto.ProtocolMessage_REVOKE:
		event.Type = messageEventRevoke
	default:
		return MessageEvent{}, false
	}
	if event.EventID == "" {
		event.EventID = fmt.Sprintf("%s-%s-%d", event.Type, event.TargetMessageID, event.Timestamp.UnixNano())
	}
	return event, true
}

func mediaCaption(msg *waProto.Message) string {
	switch {
	case msg.GetImageMessage() != nil:
		return msg.GetImageMessage().GetCaption()
	case msg.GetVideoMessage() != nil:
		return msg.GetVideoMessage().GetCaption()
	case msg.GetDocumentMessage() != nil:
		return msg.GetDocumentMessage().GetCaption()
	}
	return ""
}

// RecordMessageEvent appends the event (idempotent on event_id+chat_jid) and
// flags the target row with edited_at / revoked_at. It never rewrites the
// original content of the target message.
func (store *MessageStore) RecordMessageEvent(event MessageEvent) error {
	if _, err := store.db.Exec(
		`INSERT OR IGNORE INTO message_events
		(event_id, chat_jid, target_message_id, event_type, sender_jid, new_content, timestamp, is_from_me)
		VALUES (?, ?, ?, ?, ?, ?, ?, ?)`,
		event.EventID, event.ChatJID, event.TargetMessageID, event.Type,
		nullableString(event.SenderJID), nullableString(event.NewContent), event.Timestamp, event.IsFromMe,
	); err != nil {
		return fmt.Errorf("store message event: %w", err)
	}

	column := "edited_at"
	if event.Type == messageEventRevoke {
		column = "revoked_at"
	}
	// Keep the most recent event time. Try the event's chat first, then its
	// verified LID/PN aliases (the target may have been stored under the other
	// JID of the same 1:1 chat). Never match by message ID alone: an ID can
	// exist in an unrelated chat, which must not be flagged.
	update := fmt.Sprintf(
		`UPDATE messages SET %[1]s = CASE WHEN %[1]s IS NULL OR %[1]s < ? THEN ? ELSE %[1]s END
		WHERE id = ? AND chat_jid = ?`, column)
	for _, chat := range append([]string{event.ChatJID}, event.AliasChatJIDs...) {
		if chat == "" {
			continue
		}
		res, err := store.db.Exec(update, event.Timestamp, event.Timestamp, event.TargetMessageID, chat)
		if err != nil {
			return fmt.Errorf("flag %s: %w", column, err)
		}
		if n, _ := res.RowsAffected(); n > 0 {
			return nil
		}
	}
	// The target is not stored yet (events can arrive before their message,
	// e.g. during history sync). StoreMessage applies pending events when the
	// target is inserted.
	return nil
}

// chatAliases returns the other JIDs that the device's LID map verifies as the
// same 1:1 chat (LID <-> phone number). Groups and unknown mappings have none.
func chatAliases(client *whatsmeow.Client, chatJID string) []string {
	if client == nil || client.Store == nil || client.Store.LIDs == nil {
		return nil
	}
	jid, err := types.ParseJID(chatJID)
	if err != nil {
		return nil
	}
	return replyChatCandidates(client, jid)[1:]
}

// reconcilePendingEvents flags a just-stored message with edit/revoke events
// that were recorded before it existed. Runs inside the StoreMessage
// transaction so the message never becomes visible without its flags.
func reconcilePendingEvents(tx *sql.Tx, id, chatJID string) error {
	for _, ev := range []struct{ eventType, column string }{
		{messageEventEdit, "edited_at"},
		{messageEventRevoke, "revoked_at"},
	} {
		stmt := fmt.Sprintf(
			`UPDATE messages SET %[1]s = (
				SELECT MAX(timestamp) FROM message_events
				WHERE target_message_id = ? AND chat_jid = ? AND event_type = ?)
			WHERE id = ? AND chat_jid = ?
			AND (%[1]s IS NULL OR %[1]s < (
				SELECT MAX(timestamp) FROM message_events
				WHERE target_message_id = ? AND chat_jid = ? AND event_type = ?))`, ev.column)
		if _, err := tx.Exec(stmt,
			id, chatJID, ev.eventType,
			id, chatJID,
			id, chatJID, ev.eventType,
		); err != nil {
			return fmt.Errorf("reconcile %s: %w", ev.column, err)
		}
	}
	return nil
}
