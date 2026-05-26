package main

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"strconv"
	"time"

	"go.mau.fi/whatsmeow"
	"go.mau.fi/whatsmeow/appstate"
	"go.mau.fi/whatsmeow/types"
	"go.mau.fi/whatsmeow/types/events"
	waLog "go.mau.fi/whatsmeow/util/log"
)

// migrateLabelTables creates label-related tables for clients that pre-date this feature.
// CREATE TABLE IF NOT EXISTS is safe to run on every startup.
func migrateLabelTables(store *MessageStore) error {
	_, err := store.db.Exec(`
		CREATE TABLE IF NOT EXISTS labels (
			id TEXT PRIMARY KEY,
			name TEXT NOT NULL DEFAULT '',
			color INTEGER NOT NULL DEFAULT 0,
			deleted BOOLEAN NOT NULL DEFAULT FALSE,
			updated_at TIMESTAMP
		);
		CREATE TABLE IF NOT EXISTS label_chats (
			label_id TEXT NOT NULL,
			chat_jid TEXT NOT NULL,
			PRIMARY KEY (label_id, chat_jid)
		);
		CREATE TABLE IF NOT EXISTS label_messages (
			label_id TEXT NOT NULL,
			chat_jid TEXT NOT NULL,
			message_id TEXT NOT NULL,
			PRIMARY KEY (label_id, chat_jid, message_id)
		);
	`)
	return err
}

// --- Event handlers (called from main.go event loop) ---

func handleLabelEdit(store *MessageStore, evt *events.LabelEdit, logger waLog.Logger) {
	if evt.Action == nil {
		return
	}
	name := evt.Action.GetName()
	color := evt.Action.GetColor()
	deleted := false
	if evt.Action.Deleted != nil {
		deleted = *evt.Action.Deleted
	}
	_, err := store.db.Exec(
		`INSERT INTO labels (id, name, color, deleted, updated_at) VALUES (?, ?, ?, ?, ?)
		 ON CONFLICT(id) DO UPDATE SET name=excluded.name, color=excluded.color, deleted=excluded.deleted, updated_at=excluded.updated_at`,
		evt.LabelID, name, color, deleted, evt.Timestamp,
	)
	if err != nil {
		logger.Warnf("Failed to store label %s: %v", evt.LabelID, err)
	}
}

func handleLabelAssociationChat(store *MessageStore, evt *events.LabelAssociationChat, logger waLog.Logger) {
	if evt.Action == nil {
		return
	}
	chatJID := evt.JID.String()
	var err error
	if evt.Action.GetLabeled() {
		_, err = store.db.Exec(
			`INSERT OR IGNORE INTO label_chats (label_id, chat_jid) VALUES (?, ?)`,
			evt.LabelID, chatJID,
		)
	} else {
		_, err = store.db.Exec(
			`DELETE FROM label_chats WHERE label_id = ? AND chat_jid = ?`,
			evt.LabelID, chatJID,
		)
	}
	if err != nil {
		logger.Warnf("Failed to update label-chat association %s/%s: %v", evt.LabelID, chatJID, err)
	}
}

func handleLabelAssociationMessage(store *MessageStore, evt *events.LabelAssociationMessage, logger waLog.Logger) {
	if evt.Action == nil {
		return
	}
	chatJID := evt.JID.String()
	var err error
	if evt.Action.GetLabeled() {
		_, err = store.db.Exec(
			`INSERT OR IGNORE INTO label_messages (label_id, chat_jid, message_id) VALUES (?, ?, ?)`,
			evt.LabelID, chatJID, evt.MessageID,
		)
	} else {
		_, err = store.db.Exec(
			`DELETE FROM label_messages WHERE label_id = ? AND chat_jid = ? AND message_id = ?`,
			evt.LabelID, chatJID, evt.MessageID,
		)
	}
	if err != nil {
		logger.Warnf("Failed to update label-message association %s/%s/%s: %v", evt.LabelID, chatJID, evt.MessageID, err)
	}
}

// --- HTTP request/response types ---

type LabelInfo struct {
	ID        string    `json:"id"`
	Name      string    `json:"name"`
	Color     int32     `json:"color"`
	Deleted   bool      `json:"deleted"`
	UpdatedAt time.Time `json:"updated_at,omitempty"`
}

type ListLabelsResponse struct {
	Success bool        `json:"success"`
	Message string      `json:"message,omitempty"`
	Labels  []LabelInfo `json:"labels"`
}

type LabelChatsResponse struct {
	Success bool     `json:"success"`
	Message string   `json:"message,omitempty"`
	Chats   []string `json:"chats"`
}

type LabelMessagesResponse struct {
	Success  bool                `json:"success"`
	Message  string              `json:"message,omitempty"`
	Messages []LabelMessageEntry `json:"messages"`
}

type LabelMessageEntry struct {
	ChatJID   string `json:"chat_jid"`
	MessageID string `json:"message_id"`
}

type EditLabelRequest struct {
	LabelID string `json:"label_id"`
	Name    string `json:"name"`
	Color   int32  `json:"color"`
	Deleted bool   `json:"deleted"`
}

type EditLabelResponse struct {
	Success bool   `json:"success"`
	Message string `json:"message"`
	LabelID string `json:"label_id"`
}

type LabelChatRequest struct {
	LabelID string `json:"label_id"`
	ChatJID string `json:"chat_jid"`
	Labeled bool   `json:"labeled"`
}

type LabelMessageRequest struct {
	LabelID   string `json:"label_id"`
	ChatJID   string `json:"chat_jid"`
	MessageID string `json:"message_id"`
	Labeled   bool   `json:"labeled"`
}

// GenericResponse is the shared success/message shape for endpoints that don't
// return additional data. Kept here (rather than messaging.go) to avoid forward
// references; other files reuse it.
type GenericResponse struct {
	Success bool   `json:"success"`
	Message string `json:"message"`
}

// --- HTTP handlers ---

func registerLabelRoutes(client *whatsmeow.Client, store *MessageStore) {
	http.HandleFunc("/api/labels", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet {
			http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		includeDeleted := r.URL.Query().Get("include_deleted") == "true"

		query := `SELECT id, name, color, deleted, COALESCE(updated_at, '') FROM labels`
		if !includeDeleted {
			query += ` WHERE deleted = 0`
		}
		query += ` ORDER BY name, id`

		rows, err := store.db.Query(query)
		if err != nil {
			w.WriteHeader(http.StatusInternalServerError)
			json.NewEncoder(w).Encode(ListLabelsResponse{Success: false, Message: err.Error()})
			return
		}
		defer rows.Close()

		labels := []LabelInfo{}
		for rows.Next() {
			var li LabelInfo
			var updated string
			if err := rows.Scan(&li.ID, &li.Name, &li.Color, &li.Deleted, &updated); err != nil {
				w.WriteHeader(http.StatusInternalServerError)
				json.NewEncoder(w).Encode(ListLabelsResponse{Success: false, Message: err.Error()})
				return
			}
			if updated != "" {
				if t, perr := time.Parse(time.RFC3339Nano, updated); perr == nil {
					li.UpdatedAt = t
				}
			}
			labels = append(labels, li)
		}
		json.NewEncoder(w).Encode(ListLabelsResponse{Success: true, Labels: labels})
	})

	http.HandleFunc("/api/labels/chats", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet {
			http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		labelID := r.URL.Query().Get("label_id")
		if labelID == "" {
			w.WriteHeader(http.StatusBadRequest)
			json.NewEncoder(w).Encode(LabelChatsResponse{Success: false, Message: "label_id is required"})
			return
		}
		rows, err := store.db.Query(`SELECT chat_jid FROM label_chats WHERE label_id = ? ORDER BY chat_jid`, labelID)
		if err != nil {
			w.WriteHeader(http.StatusInternalServerError)
			json.NewEncoder(w).Encode(LabelChatsResponse{Success: false, Message: err.Error()})
			return
		}
		defer rows.Close()
		chats := []string{}
		for rows.Next() {
			var jid string
			if err := rows.Scan(&jid); err == nil {
				chats = append(chats, jid)
			}
		}
		json.NewEncoder(w).Encode(LabelChatsResponse{Success: true, Chats: chats})
	})

	http.HandleFunc("/api/labels/messages", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet {
			http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		labelID := r.URL.Query().Get("label_id")
		if labelID == "" {
			w.WriteHeader(http.StatusBadRequest)
			json.NewEncoder(w).Encode(LabelMessagesResponse{Success: false, Message: "label_id is required"})
			return
		}
		rows, err := store.db.Query(`SELECT chat_jid, message_id FROM label_messages WHERE label_id = ?`, labelID)
		if err != nil {
			w.WriteHeader(http.StatusInternalServerError)
			json.NewEncoder(w).Encode(LabelMessagesResponse{Success: false, Message: err.Error()})
			return
		}
		defer rows.Close()
		out := []LabelMessageEntry{}
		for rows.Next() {
			var e LabelMessageEntry
			if err := rows.Scan(&e.ChatJID, &e.MessageID); err == nil {
				out = append(out, e)
			}
		}
		json.NewEncoder(w).Encode(LabelMessagesResponse{Success: true, Messages: out})
	})

	http.HandleFunc("/api/labels/edit", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		var req EditLabelRequest
		if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
			w.WriteHeader(http.StatusBadRequest)
			json.NewEncoder(w).Encode(EditLabelResponse{Success: false, Message: "Invalid request format"})
			return
		}
		if req.LabelID == "" {
			// WhatsApp Business labels use numeric string IDs; generate one
			// based on the current timestamp. The user-facing display order
			// only matters for labels with overlapping names.
			req.LabelID = strconv.FormatInt(time.Now().UnixMilli(), 10)
		}
		patch := appstate.BuildLabelEdit(req.LabelID, req.Name, req.Color, req.Deleted)
		if err := client.SendAppState(context.Background(), patch); err != nil {
			w.WriteHeader(http.StatusInternalServerError)
			json.NewEncoder(w).Encode(EditLabelResponse{Success: false, Message: err.Error(), LabelID: req.LabelID})
			return
		}
		// Optimistic local update; the app-state echo will reconcile if anything diverges.
		_, _ = store.db.Exec(
			`INSERT INTO labels (id, name, color, deleted, updated_at) VALUES (?, ?, ?, ?, ?)
			 ON CONFLICT(id) DO UPDATE SET name=excluded.name, color=excluded.color, deleted=excluded.deleted, updated_at=excluded.updated_at`,
			req.LabelID, req.Name, req.Color, req.Deleted, time.Now(),
		)
		json.NewEncoder(w).Encode(EditLabelResponse{Success: true, Message: "Label saved", LabelID: req.LabelID})
	})

	http.HandleFunc("/api/labels/chat", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		var req LabelChatRequest
		if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
			w.WriteHeader(http.StatusBadRequest)
			json.NewEncoder(w).Encode(GenericResponse{Success: false, Message: "Invalid request format"})
			return
		}
		if req.LabelID == "" || req.ChatJID == "" {
			w.WriteHeader(http.StatusBadRequest)
			json.NewEncoder(w).Encode(GenericResponse{Success: false, Message: "label_id and chat_jid are required"})
			return
		}
		chatJID, err := types.ParseJID(req.ChatJID)
		if err != nil {
			w.WriteHeader(http.StatusBadRequest)
			json.NewEncoder(w).Encode(GenericResponse{Success: false, Message: fmt.Sprintf("Invalid chat_jid: %v", err)})
			return
		}
		patch := appstate.BuildLabelChat(chatJID, req.LabelID, req.Labeled)
		if err := client.SendAppState(context.Background(), patch); err != nil {
			w.WriteHeader(http.StatusInternalServerError)
			json.NewEncoder(w).Encode(GenericResponse{Success: false, Message: err.Error()})
			return
		}
		if req.Labeled {
			_, _ = store.db.Exec(`INSERT OR IGNORE INTO label_chats (label_id, chat_jid) VALUES (?, ?)`, req.LabelID, req.ChatJID)
		} else {
			_, _ = store.db.Exec(`DELETE FROM label_chats WHERE label_id = ? AND chat_jid = ?`, req.LabelID, req.ChatJID)
		}
		action := "labeled"
		if !req.Labeled {
			action = "unlabeled"
		}
		json.NewEncoder(w).Encode(GenericResponse{Success: true, Message: fmt.Sprintf("Chat %s with label %s", action, req.LabelID)})
	})

	http.HandleFunc("/api/labels/message", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		var req LabelMessageRequest
		if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
			w.WriteHeader(http.StatusBadRequest)
			json.NewEncoder(w).Encode(GenericResponse{Success: false, Message: "Invalid request format"})
			return
		}
		if req.LabelID == "" || req.ChatJID == "" || req.MessageID == "" {
			w.WriteHeader(http.StatusBadRequest)
			json.NewEncoder(w).Encode(GenericResponse{Success: false, Message: "label_id, chat_jid and message_id are required"})
			return
		}
		chatJID, err := types.ParseJID(req.ChatJID)
		if err != nil {
			w.WriteHeader(http.StatusBadRequest)
			json.NewEncoder(w).Encode(GenericResponse{Success: false, Message: fmt.Sprintf("Invalid chat_jid: %v", err)})
			return
		}
		patch := appstate.BuildLabelMessage(chatJID, req.LabelID, req.MessageID, req.Labeled)
		if err := client.SendAppState(context.Background(), patch); err != nil {
			w.WriteHeader(http.StatusInternalServerError)
			json.NewEncoder(w).Encode(GenericResponse{Success: false, Message: err.Error()})
			return
		}
		if req.Labeled {
			_, _ = store.db.Exec(`INSERT OR IGNORE INTO label_messages (label_id, chat_jid, message_id) VALUES (?, ?, ?)`, req.LabelID, req.ChatJID, req.MessageID)
		} else {
			_, _ = store.db.Exec(`DELETE FROM label_messages WHERE label_id = ? AND chat_jid = ? AND message_id = ?`, req.LabelID, req.ChatJID, req.MessageID)
		}
		action := "labeled"
		if !req.Labeled {
			action = "unlabeled"
		}
		json.NewEncoder(w).Encode(GenericResponse{Success: true, Message: fmt.Sprintf("Message %s with label %s", action, req.LabelID)})
	})
}
