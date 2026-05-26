package main

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"time"

	"go.mau.fi/whatsmeow"
	waProto "go.mau.fi/whatsmeow/binary/proto"
	"go.mau.fi/whatsmeow/types"
	"google.golang.org/protobuf/proto"
)

// --- Request/response types ---

type EditMessageRequest struct {
	ChatJID    string `json:"chat_jid"`
	MessageID  string `json:"message_id"`
	NewContent string `json:"new_content"`
}

type DeleteMessageRequest struct {
	ChatJID   string `json:"chat_jid"`
	MessageID string `json:"message_id"`
}

type ReactToMessageRequest struct {
	ChatJID   string `json:"chat_jid"`
	MessageID string `json:"message_id"`
	// SenderJID identifies the original sender of the message being reacted to.
	// For your own messages, leave empty (or set is_from_me=true). For incoming
	// DMs, this equals chat_jid. For groups, it's the participant JID.
	SenderJID string `json:"sender_jid"`
	IsFromMe  bool   `json:"is_from_me"`
	Emoji     string `json:"emoji"` // empty string removes the reaction
}

type MarkReadRequest struct {
	ChatJID    string   `json:"chat_jid"`
	MessageIDs []string `json:"message_ids"`
	// SenderJID of the messages being read. For DMs this equals chat_jid; for
	// groups it's the participant. If omitted, the bridge tries to derive it
	// from the local message store.
	SenderJID string `json:"sender_jid"`
}

type TypingIndicatorRequest struct {
	ChatJID          string `json:"chat_jid"`
	IsTyping         bool   `json:"is_typing"`
	IsRecordingAudio bool   `json:"is_recording_audio"`
}

type CreatePollRequest struct {
	ChatJID               string   `json:"chat_jid"`
	Name                  string   `json:"name"`
	Options               []string `json:"options"`
	SelectableOptionCount int      `json:"selectable_option_count"` // 1 = single choice; >1 = multi; 0 = unlimited
}

type CheckOnWhatsAppRequest struct {
	// Phone numbers in E.164 format (with leading +).
	Phones []string `json:"phones"`
}

type CheckOnWhatsAppEntry struct {
	Query        string `json:"query"`
	JID          string `json:"jid"`
	IsOnWhatsApp bool   `json:"is_on_whatsapp"`
	VerifiedName string `json:"verified_name,omitempty"`
}

type CheckOnWhatsAppResponse struct {
	Success bool                   `json:"success"`
	Message string                 `json:"message,omitempty"`
	Results []CheckOnWhatsAppEntry `json:"results"`
}

type DisappearingTimerRequest struct {
	ChatJID string `json:"chat_jid"`
	// Accepted values: "off", "24h", "7d", "90d" (see whatsmeow ParseDisappearingTimerString).
	Timer string `json:"timer"`
}

// --- HTTP handlers ---

func registerMessagingRoutes(client *whatsmeow.Client, store *MessageStore) {
	http.HandleFunc("/api/messages/edit", func(w http.ResponseWriter, r *http.Request) {
		writeJSON := newJSONWriter(w)
		if r.Method != http.MethodPost {
			writeJSON(http.StatusMethodNotAllowed, GenericResponse{Success: false, Message: "Method not allowed"})
			return
		}
		var req EditMessageRequest
		if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
			writeJSON(http.StatusBadRequest, GenericResponse{Success: false, Message: "Invalid request format"})
			return
		}
		if req.ChatJID == "" || req.MessageID == "" {
			writeJSON(http.StatusBadRequest, GenericResponse{Success: false, Message: "chat_jid and message_id are required"})
			return
		}
		chatJID, err := types.ParseJID(req.ChatJID)
		if err != nil {
			writeJSON(http.StatusBadRequest, GenericResponse{Success: false, Message: fmt.Sprintf("Invalid chat_jid: %v", err)})
			return
		}
		newMsg := &waProto.Message{Conversation: proto.String(req.NewContent)}
		edit := client.BuildEdit(chatJID, req.MessageID, newMsg)
		if _, err := client.SendMessage(context.Background(), chatJID, edit); err != nil {
			writeJSON(http.StatusInternalServerError, GenericResponse{Success: false, Message: err.Error()})
			return
		}
		// Reflect the edit locally so list_messages shows the new content.
		_, _ = store.db.Exec(
			`UPDATE messages SET content = ? WHERE id = ? AND chat_jid = ?`,
			req.NewContent, req.MessageID, req.ChatJID,
		)
		writeJSON(http.StatusOK, GenericResponse{Success: true, Message: "Message edited"})
	})

	http.HandleFunc("/api/messages/delete", func(w http.ResponseWriter, r *http.Request) {
		writeJSON := newJSONWriter(w)
		if r.Method != http.MethodPost {
			writeJSON(http.StatusMethodNotAllowed, GenericResponse{Success: false, Message: "Method not allowed"})
			return
		}
		var req DeleteMessageRequest
		if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
			writeJSON(http.StatusBadRequest, GenericResponse{Success: false, Message: "Invalid request format"})
			return
		}
		if req.ChatJID == "" || req.MessageID == "" {
			writeJSON(http.StatusBadRequest, GenericResponse{Success: false, Message: "chat_jid and message_id are required"})
			return
		}
		chatJID, err := types.ParseJID(req.ChatJID)
		if err != nil {
			writeJSON(http.StatusBadRequest, GenericResponse{Success: false, Message: fmt.Sprintf("Invalid chat_jid: %v", err)})
			return
		}
		if _, err := client.RevokeMessage(context.Background(), chatJID, req.MessageID); err != nil {
			writeJSON(http.StatusInternalServerError, GenericResponse{Success: false, Message: err.Error()})
			return
		}
		// Mark locally so the deleted message no longer shows up with content.
		_, _ = store.db.Exec(
			`UPDATE messages SET content = '' WHERE id = ? AND chat_jid = ?`,
			req.MessageID, req.ChatJID,
		)
		writeJSON(http.StatusOK, GenericResponse{Success: true, Message: "Message deleted"})
	})

	http.HandleFunc("/api/messages/react", func(w http.ResponseWriter, r *http.Request) {
		writeJSON := newJSONWriter(w)
		if r.Method != http.MethodPost {
			writeJSON(http.StatusMethodNotAllowed, GenericResponse{Success: false, Message: "Method not allowed"})
			return
		}
		var req ReactToMessageRequest
		if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
			writeJSON(http.StatusBadRequest, GenericResponse{Success: false, Message: "Invalid request format"})
			return
		}
		if req.ChatJID == "" || req.MessageID == "" {
			writeJSON(http.StatusBadRequest, GenericResponse{Success: false, Message: "chat_jid and message_id are required"})
			return
		}
		chatJID, err := types.ParseJID(req.ChatJID)
		if err != nil {
			writeJSON(http.StatusBadRequest, GenericResponse{Success: false, Message: fmt.Sprintf("Invalid chat_jid: %v", err)})
			return
		}
		var senderJID types.JID
		if req.IsFromMe {
			senderJID = types.EmptyJID
		} else if req.SenderJID != "" {
			senderJID, err = types.ParseJID(req.SenderJID)
			if err != nil {
				writeJSON(http.StatusBadRequest, GenericResponse{Success: false, Message: fmt.Sprintf("Invalid sender_jid: %v", err)})
				return
			}
		} else {
			// Default: for DMs the sender of an incoming message is the chat
			// itself; for groups the caller must supply sender_jid.
			senderJID = chatJID
		}
		reaction := client.BuildReaction(chatJID, senderJID, req.MessageID, req.Emoji)
		if _, err := client.SendMessage(context.Background(), chatJID, reaction); err != nil {
			writeJSON(http.StatusInternalServerError, GenericResponse{Success: false, Message: err.Error()})
			return
		}
		msg := "Reaction sent"
		if req.Emoji == "" {
			msg = "Reaction removed"
		}
		writeJSON(http.StatusOK, GenericResponse{Success: true, Message: msg})
	})

	http.HandleFunc("/api/messages/mark-read", func(w http.ResponseWriter, r *http.Request) {
		writeJSON := newJSONWriter(w)
		if r.Method != http.MethodPost {
			writeJSON(http.StatusMethodNotAllowed, GenericResponse{Success: false, Message: "Method not allowed"})
			return
		}
		var req MarkReadRequest
		if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
			writeJSON(http.StatusBadRequest, GenericResponse{Success: false, Message: "Invalid request format"})
			return
		}
		if req.ChatJID == "" || len(req.MessageIDs) == 0 {
			writeJSON(http.StatusBadRequest, GenericResponse{Success: false, Message: "chat_jid and at least one message_id are required"})
			return
		}
		chatJID, err := types.ParseJID(req.ChatJID)
		if err != nil {
			writeJSON(http.StatusBadRequest, GenericResponse{Success: false, Message: fmt.Sprintf("Invalid chat_jid: %v", err)})
			return
		}
		var senderJID types.JID
		if req.SenderJID != "" {
			senderJID, err = types.ParseJID(req.SenderJID)
			if err != nil {
				writeJSON(http.StatusBadRequest, GenericResponse{Success: false, Message: fmt.Sprintf("Invalid sender_jid: %v", err)})
				return
			}
		} else {
			// Try to recover the sender from the local store; if not found,
			// fall back to chat_jid (which is correct for DMs).
			var dbSender string
			if err := store.db.QueryRow(
				`SELECT sender FROM messages WHERE id = ? AND chat_jid = ? LIMIT 1`,
				req.MessageIDs[0], req.ChatJID,
			).Scan(&dbSender); err == nil && dbSender != "" {
				if parsed, perr := types.ParseJID(dbSender); perr == nil {
					senderJID = parsed
				}
			}
			if senderJID.IsEmpty() {
				senderJID = chatJID
			}
		}

		ids := make([]types.MessageID, len(req.MessageIDs))
		for i, id := range req.MessageIDs {
			ids[i] = types.MessageID(id)
		}
		if err := client.MarkRead(context.Background(), ids, time.Now(), chatJID, senderJID); err != nil {
			writeJSON(http.StatusInternalServerError, GenericResponse{Success: false, Message: err.Error()})
			return
		}
		// Reflect locally.
		for _, id := range req.MessageIDs {
			_, _ = store.db.Exec(
				`UPDATE messages SET is_read = TRUE WHERE id = ? AND chat_jid = ?`,
				id, req.ChatJID,
			)
		}
		writeJSON(http.StatusOK, GenericResponse{Success: true, Message: fmt.Sprintf("Marked %d message(s) as read", len(req.MessageIDs))})
	})

	http.HandleFunc("/api/messages/typing", func(w http.ResponseWriter, r *http.Request) {
		writeJSON := newJSONWriter(w)
		if r.Method != http.MethodPost {
			writeJSON(http.StatusMethodNotAllowed, GenericResponse{Success: false, Message: "Method not allowed"})
			return
		}
		var req TypingIndicatorRequest
		if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
			writeJSON(http.StatusBadRequest, GenericResponse{Success: false, Message: "Invalid request format"})
			return
		}
		if req.ChatJID == "" {
			writeJSON(http.StatusBadRequest, GenericResponse{Success: false, Message: "chat_jid is required"})
			return
		}
		chatJID, err := types.ParseJID(req.ChatJID)
		if err != nil {
			writeJSON(http.StatusBadRequest, GenericResponse{Success: false, Message: fmt.Sprintf("Invalid chat_jid: %v", err)})
			return
		}
		state := types.ChatPresencePaused
		if req.IsTyping {
			state = types.ChatPresenceComposing
		}
		media := types.ChatPresenceMediaText
		if req.IsRecordingAudio {
			media = types.ChatPresenceMediaAudio
		}
		if err := client.SendChatPresence(context.Background(), chatJID, state, media); err != nil {
			writeJSON(http.StatusInternalServerError, GenericResponse{Success: false, Message: err.Error()})
			return
		}
		writeJSON(http.StatusOK, GenericResponse{Success: true, Message: "Presence sent"})
	})

	http.HandleFunc("/api/messages/poll", func(w http.ResponseWriter, r *http.Request) {
		writeJSON := newJSONWriter(w)
		if r.Method != http.MethodPost {
			writeJSON(http.StatusMethodNotAllowed, GenericResponse{Success: false, Message: "Method not allowed"})
			return
		}
		var req CreatePollRequest
		if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
			writeJSON(http.StatusBadRequest, GenericResponse{Success: false, Message: "Invalid request format"})
			return
		}
		if req.ChatJID == "" || req.Name == "" || len(req.Options) < 2 {
			writeJSON(http.StatusBadRequest, GenericResponse{Success: false, Message: "chat_jid, name and at least 2 options are required"})
			return
		}
		chatJID, err := types.ParseJID(req.ChatJID)
		if err != nil {
			writeJSON(http.StatusBadRequest, GenericResponse{Success: false, Message: fmt.Sprintf("Invalid chat_jid: %v", err)})
			return
		}
		poll := client.BuildPollCreation(req.Name, req.Options, req.SelectableOptionCount)
		if _, err := client.SendMessage(context.Background(), chatJID, poll); err != nil {
			writeJSON(http.StatusInternalServerError, GenericResponse{Success: false, Message: err.Error()})
			return
		}
		writeJSON(http.StatusOK, GenericResponse{Success: true, Message: "Poll sent"})
	})

	http.HandleFunc("/api/contacts/check", func(w http.ResponseWriter, r *http.Request) {
		writeJSON := newJSONWriter(w)
		if r.Method != http.MethodPost {
			writeJSON(http.StatusMethodNotAllowed, CheckOnWhatsAppResponse{Success: false, Message: "Method not allowed"})
			return
		}
		var req CheckOnWhatsAppRequest
		if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
			writeJSON(http.StatusBadRequest, CheckOnWhatsAppResponse{Success: false, Message: "Invalid request format"})
			return
		}
		if len(req.Phones) == 0 {
			writeJSON(http.StatusBadRequest, CheckOnWhatsAppResponse{Success: false, Message: "phones is required"})
			return
		}
		results, err := client.IsOnWhatsApp(context.Background(), req.Phones)
		if err != nil {
			writeJSON(http.StatusInternalServerError, CheckOnWhatsAppResponse{Success: false, Message: err.Error()})
			return
		}
		out := make([]CheckOnWhatsAppEntry, 0, len(results))
		for _, r := range results {
			entry := CheckOnWhatsAppEntry{
				Query:        r.Query,
				JID:          r.JID.String(),
				IsOnWhatsApp: r.IsIn,
			}
			if r.VerifiedName != nil && r.VerifiedName.Details != nil && r.VerifiedName.Details.VerifiedName != nil {
				entry.VerifiedName = *r.VerifiedName.Details.VerifiedName
			}
			out = append(out, entry)
		}
		writeJSON(http.StatusOK, CheckOnWhatsAppResponse{Success: true, Results: out})
	})

	http.HandleFunc("/api/chats/disappearing-timer", func(w http.ResponseWriter, r *http.Request) {
		writeJSON := newJSONWriter(w)
		if r.Method != http.MethodPost {
			writeJSON(http.StatusMethodNotAllowed, GenericResponse{Success: false, Message: "Method not allowed"})
			return
		}
		var req DisappearingTimerRequest
		if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
			writeJSON(http.StatusBadRequest, GenericResponse{Success: false, Message: "Invalid request format"})
			return
		}
		if req.ChatJID == "" {
			writeJSON(http.StatusBadRequest, GenericResponse{Success: false, Message: "chat_jid is required"})
			return
		}
		chatJID, err := types.ParseJID(req.ChatJID)
		if err != nil {
			writeJSON(http.StatusBadRequest, GenericResponse{Success: false, Message: fmt.Sprintf("Invalid chat_jid: %v", err)})
			return
		}
		dur, ok := whatsmeow.ParseDisappearingTimerString(req.Timer)
		if !ok {
			writeJSON(http.StatusBadRequest, GenericResponse{Success: false, Message: "timer must be one of: off, 24h, 7d, 90d"})
			return
		}
		if err := client.SetDisappearingTimer(context.Background(), chatJID, dur, time.Now()); err != nil {
			writeJSON(http.StatusInternalServerError, GenericResponse{Success: false, Message: err.Error()})
			return
		}
		writeJSON(http.StatusOK, GenericResponse{Success: true, Message: "Disappearing timer updated"})
	})
}

// newJSONWriter returns a closure that sets the Content-Type, writes the
// status code, and JSON-encodes the body — collapsing the three-line ritual
// used everywhere into a single call.
func newJSONWriter(w http.ResponseWriter) func(status int, body interface{}) {
	w.Header().Set("Content-Type", "application/json")
	return func(status int, body interface{}) {
		if status != http.StatusOK {
			w.WriteHeader(status)
		}
		_ = json.NewEncoder(w).Encode(body)
	}
}
