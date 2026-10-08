package main

import (
	"fmt"
	"time"

	"go.mau.fi/whatsmeow"
	"go.mau.fi/whatsmeow/proto/waHistorySync"
	"go.mau.fi/whatsmeow/proto/waWeb"
	"go.mau.fi/whatsmeow/types"
	"go.mau.fi/whatsmeow/types/events"
	waLog "go.mau.fi/whatsmeow/util/log"
)

// historyMessage is one storable message extracted from a history-sync conversation.
type historyMessage struct {
	ID            string
	Sender        string
	Content       string
	Timestamp     time.Time
	IsFromMe      bool
	MediaType     string
	Filename      string
	URL           string
	MediaKey      []byte
	FileSHA256    []byte
	FileEncSHA256 []byte
	FileLength    uint64
	Web           *waWeb.WebMessageInfo
}

// historyConversationMessages extracts every storable message of one history-sync
// conversation, plus the newest message time.
//
// The upstream handler skipped whole conversations when UnreadCount was missing or
// when the first message had no timestamp. Full and recent history chunks routinely
// omit UnreadCount, so most of the history was silently dropped. Here a message is
// only skipped on its own (no timestamp, or neither text nor media).
func historyConversationMessages(conv *waHistorySync.Conversation, chat types.JID, selfUser string) ([]historyMessage, time.Time) {
	var out []historyMessage
	var latest time.Time
	for _, hm := range conv.GetMessages() {
		web := hm.GetMessage()
		if web == nil {
			continue
		}
		ts := web.GetMessageTimestamp()
		if ts == 0 {
			continue
		}
		timestamp := time.Unix(int64(ts), 0)

		msg := web.GetMessage()
		content := extractTextContent(msg)
		if content == "" {
			content = mediaCaption(msg)
		}
		var m historyMessage
		if msg != nil {
			m.MediaType, m.Filename, m.URL, m.MediaKey, m.FileSHA256, m.FileEncSHA256, m.FileLength = extractMediaInfo(msg)
		}
		if content == "" && m.MediaType == "" {
			continue
		}

		key := web.GetKey()
		m.IsFromMe = key.GetFromMe()
		switch {
		case m.IsFromMe:
			m.Sender = selfUser
		case key.GetParticipant() != "":
			m.Sender = key.GetParticipant()
		case web.GetParticipant() != "":
			// Group history often carries the author here instead of in the key.
			m.Sender = web.GetParticipant()
		default:
			m.Sender = chat.User
		}
		m.ID = key.GetID()
		m.Content = content
		m.Timestamp = timestamp
		m.Web = web
		out = append(out, m)
		if timestamp.After(latest) {
			latest = timestamp
		}
	}
	return out, latest
}

func handleHistorySync(client *whatsmeow.Client, messageStore *MessageStore, historySync *events.HistorySync, logger waLog.Logger) {
	data := historySync.Data
	fmt.Printf("Received history sync event (%s) with %d conversations\n", data.GetSyncType(), len(data.GetConversations()))

	selfUser := ""
	if client != nil && client.Store != nil && client.Store.ID != nil {
		selfUser = client.Store.ID.User
	}

	syncedCount, failed := 0, 0
	for _, conversation := range data.GetConversations() {
		chatJID := conversation.GetID()
		if chatJID == "" {
			continue
		}
		jid, err := types.ParseJID(chatJID)
		if err != nil {
			logger.Warnf("Failed to parse JID %s: %v", chatJID, err)
			continue
		}

		messages, latest := historyConversationMessages(conversation, jid, selfUser)
		if !latest.IsZero() {
			name := GetChatName(client, messageStore, jid, chatJID, conversation, "", logger)
			if err := messageStore.StoreHistoryChat(chatJID, name, latest, int32(conversation.GetUnreadCount())); err != nil {
				logger.Warnf("Failed to store history chat %s: %v", chatJID, err)
			}
		}

		for _, m := range messages {
			err := messageStore.StoreMessage(m.ID, chatJID, m.Sender, m.Content, m.Timestamp, m.IsFromMe,
				m.MediaType, m.Filename, m.URL, m.MediaKey, m.FileSHA256, m.FileEncSHA256, m.FileLength,
				historyMessageMeta(client, jid, m.Web))
			if err != nil {
				failed++
				logger.Warnf("Failed to store history message %s in %s: %v", m.ID, chatJID, err)
				continue
			}
			syncedCount++
		}
	}

	fmt.Printf("History sync complete (%s). Stored %d messages, %d failed.\n", data.GetSyncType(), syncedCount, failed)
}
