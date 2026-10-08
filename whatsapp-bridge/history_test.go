package main

import (
	"testing"
	"time"

	waProto "go.mau.fi/whatsmeow/binary/proto"
	"go.mau.fi/whatsmeow/proto/waCommon"
	"go.mau.fi/whatsmeow/proto/waHistorySync"
	"go.mau.fi/whatsmeow/proto/waWeb"
	"go.mau.fi/whatsmeow/types"
	"google.golang.org/protobuf/proto"
)

func histMsg(id string, ts uint64, fromMe bool, participant, keyParticipant string, msg *waProto.Message) *waHistorySync.HistorySyncMsg {
	key := &waCommon.MessageKey{ID: proto.String(id), FromMe: proto.Bool(fromMe)}
	if keyParticipant != "" {
		key.Participant = proto.String(keyParticipant)
	}
	web := &waWeb.WebMessageInfo{Key: key, Message: msg}
	if ts != 0 {
		web.MessageTimestamp = proto.Uint64(ts)
	}
	if participant != "" {
		web.Participant = proto.String(participant)
	}
	return &waHistorySync.HistorySyncMsg{Message: web}
}

func TestHistoryConversationKeepsMessagesWithoutUnreadCount(t *testing.T) {
	group := types.NewJID("120363000000000009", types.GroupServer)
	conv := &waHistorySync.Conversation{
		ID: proto.String(group.String()),
		// UnreadCount deliberately unset: full/recent chunks omit it.
		Messages: []*waHistorySync.HistorySyncMsg{
			histMsg("NOTS0000000001", 0, false, "", "", &waProto.Message{Conversation: proto.String("sem data")}), // first message without timestamp
			histMsg("TEXT0000000001", 1760000000, false, "", "5547999990001@s.whatsapp.net", &waProto.Message{Conversation: proto.String("oi síndico")}),
			histMsg("CAPT0000000001", 1760000100, false, "5547999990002@s.whatsapp.net", "", &waProto.Message{ImageMessage: &waProto.ImageMessage{Caption: proto.String("aviso do elevador"), Mimetype: proto.String("image/jpeg")}}),
			histMsg("MINE0000000001", 1760000200, true, "", "", &waProto.Message{ExtendedTextMessage: &waProto.ExtendedTextMessage{Text: proto.String("combinado")}}),
			histMsg("EMPT0000000001", 1760000300, false, "", "", &waProto.Message{}), // nothing to store
			{Message: nil},
		},
	}

	msgs, latest := historyConversationMessages(conv, group, "5547999990000")
	if len(msgs) != 3 {
		t.Fatalf("got %d messages, want 3: %+v", len(msgs), msgs)
	}
	if !latest.Equal(time.Unix(1760000200, 0)) {
		t.Fatalf("latest = %v", latest)
	}
	byID := map[string]historyMessage{}
	for _, m := range msgs {
		byID[m.ID] = m
	}
	if m := byID["TEXT0000000001"]; m.Content != "oi síndico" || m.Sender != "5547999990001@s.whatsapp.net" {
		t.Fatalf("text message: %+v", m)
	}
	if m := byID["CAPT0000000001"]; m.Content != "aviso do elevador" || m.MediaType != "image" || m.Sender != "5547999990002@s.whatsapp.net" {
		t.Fatalf("captioned image (author only in WebMessageInfo.Participant): %+v", m)
	}
	if m := byID["MINE0000000001"]; !m.IsFromMe || m.Sender != "5547999990000" || m.Content != "combinado" {
		t.Fatalf("own message: %+v", m)
	}
}

func TestStoreHistoryChatNeverMovesBackwards(t *testing.T) {
	store, err := NewMessageStore(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	defer store.Close()
	jid := "120363000000000010@g.us"
	newer := time.Date(2026, 10, 8, 12, 0, 0, 0, time.UTC)
	if err := store.StoreHistoryChat(jid, "Condomínio Azul", newer, 2); err != nil {
		t.Fatal(err)
	}
	if err := store.StoreHistoryChat(jid, "", newer.AddDate(0, -3, 0), 0); err != nil { // older chunk, no name
		t.Fatal(err)
	}
	var name string
	var last time.Time
	if err := store.db.QueryRow(`SELECT name, last_message_time FROM chats WHERE jid = ?`, jid).Scan(&name, &last); err != nil {
		t.Fatal(err)
	}
	if name != "Condomínio Azul" || !last.Equal(newer) {
		t.Fatalf("chat regressed: name=%q last=%v", name, last)
	}
}

func TestLogoutFlag(t *testing.T) {
	cfg, err := loadBridgeConfig([]string{"-logout"}, envMap(nil))
	if err != nil || !cfg.Logout {
		t.Fatalf("-logout not parsed: %+v %v", cfg, err)
	}
	if cfg, _ := loadBridgeConfig(nil, envMap(nil)); cfg.Logout {
		t.Fatal("logout must be off by default")
	}
}
