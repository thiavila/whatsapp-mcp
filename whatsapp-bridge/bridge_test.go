package main

import (
	"database/sql"
	"encoding/json"
	"fmt"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"go.mau.fi/whatsmeow"
	waProto "go.mau.fi/whatsmeow/binary/proto"
	"go.mau.fi/whatsmeow/types"
	"google.golang.org/protobuf/proto"
)

// Synthetic identifiers only — never real numbers or JIDs.
const (
	testToken       = "test-token-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
	otherTestToken  = "test-token-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
	testGroupJID    = "120363000000000001@g.us"
	testLIDUser     = "100000000000001"
	testPhoneUser   = "5500000000001"
	testMentionJID  = "100000000000002@lid"
	testQuotedMsgID = "QUOTEDMSG0000001"
)

func envMap(values map[string]string) func(string) string {
	return func(key string) string { return values[key] }
}

// --- BR-1 / BR-2 / BR-8: configuration ---------------------------------------

func TestLoadBridgeConfigDefaultsMatchLegacyBehaviour(t *testing.T) {
	cfg, err := loadBridgeConfig(nil, envMap(nil))
	if err != nil {
		t.Fatal(err)
	}
	if cfg.Port != 8080 || cfg.StoreDir != "store" || cfg.Instance != "default" {
		t.Fatalf("defaults changed: %+v", cfg)
	}
	if cfg.AuthMode != authModeOff || cfg.Token != "" {
		t.Fatalf("no token must mean auth off (legacy behaviour), got %+v", cfg)
	}
}

func TestLoadBridgeConfigFromEnv(t *testing.T) {
	cfg, err := loadBridgeConfig(nil, envMap(map[string]string{
		"WHATSAPP_BRIDGE_PORT":     "8081",
		"WHATSAPP_STORE_DIR":       "/srv/wa/comercial/store",
		"WHATSAPP_BRIDGE_INSTANCE": "comercial",
		"WHATSAPP_BRIDGE_TOKEN":    testToken,
	}))
	if err != nil {
		t.Fatal(err)
	}
	if cfg.Port != 8081 || cfg.StoreDir != "/srv/wa/comercial/store" || cfg.Instance != "comercial" {
		t.Fatalf("env not applied: %+v", cfg)
	}
	if cfg.AuthMode != authModeEnforce || cfg.Token != testToken {
		t.Fatalf("a token must enforce auth by default: %+v", cfg)
	}
}

func TestLoadBridgeConfigFlagsOverrideEnv(t *testing.T) {
	cfg, err := loadBridgeConfig(
		[]string{"-port", "9090", "-store-dir", "/tmp/x", "-instance", "flagged"},
		envMap(map[string]string{"WHATSAPP_BRIDGE_PORT": "8081", "WHATSAPP_STORE_DIR": "/env", "WHATSAPP_BRIDGE_INSTANCE": "env"}),
	)
	if err != nil {
		t.Fatal(err)
	}
	if cfg.Port != 9090 || cfg.StoreDir != "/tmp/x" || cfg.Instance != "flagged" {
		t.Fatalf("flags must win over env: %+v", cfg)
	}
}

func TestLoadBridgeConfigTokenFile(t *testing.T) {
	path := filepath.Join(t.TempDir(), "token")
	if err := os.WriteFile(path, []byte(testToken+"\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	cfg, err := loadBridgeConfig(nil, envMap(map[string]string{"WHATSAPP_BRIDGE_TOKEN_FILE": path}))
	if err != nil {
		t.Fatal(err)
	}
	if cfg.Token != testToken || cfg.AuthMode != authModeEnforce {
		t.Fatalf("token file not loaded: %+v", cfg)
	}
}

func TestLoadBridgeConfigRejectsUnsafeOrIncoherentSettings(t *testing.T) {
	tokenFile := filepath.Join(t.TempDir(), "token")
	if err := os.WriteFile(tokenFile, []byte(testToken), 0o600); err != nil {
		t.Fatal(err)
	}
	cases := map[string]struct {
		args []string
		env  map[string]string
	}{
		"bad port":              {env: map[string]string{"WHATSAPP_BRIDGE_PORT": "abc"}},
		"port out of range":     {args: []string{"-port", "70000"}},
		"empty store flag":      {args: []string{"-store-dir", ""}},
		"short token":           {env: map[string]string{"WHATSAPP_BRIDGE_TOKEN": "short"}},
		"token and token file":  {env: map[string]string{"WHATSAPP_BRIDGE_TOKEN": testToken, "WHATSAPP_BRIDGE_TOKEN_FILE": tokenFile}},
		"missing token file":    {env: map[string]string{"WHATSAPP_BRIDGE_TOKEN_FILE": filepath.Join(t.TempDir(), "nope")}},
		"require without token": {args: []string{"-require-token"}},
		"require env":           {env: map[string]string{"WHATSAPP_BRIDGE_REQUIRE_TOKEN": "true"}},
		"warn without token":    {env: map[string]string{"WHATSAPP_BRIDGE_AUTH_MODE": "warn"}},
		"enforce without token": {env: map[string]string{"WHATSAPP_BRIDGE_AUTH_MODE": "enforce"}},
		"unknown auth mode":     {env: map[string]string{"WHATSAPP_BRIDGE_AUTH_MODE": "off", "WHATSAPP_BRIDGE_TOKEN": testToken}},
	}
	for name, tc := range cases {
		t.Run(name, func(t *testing.T) {
			if _, err := loadBridgeConfig(tc.args, envMap(tc.env)); err == nil {
				t.Fatal("expected a configuration error")
			}
		})
	}
}

func TestLoadBridgeConfigWarnMode(t *testing.T) {
	cfg, err := loadBridgeConfig(nil, envMap(map[string]string{
		"WHATSAPP_BRIDGE_TOKEN":     testToken,
		"WHATSAPP_BRIDGE_AUTH_MODE": "WARN",
	}))
	if err != nil {
		t.Fatal(err)
	}
	if cfg.AuthMode != authModeWarn {
		t.Fatalf("auth mode = %q, want warn", cfg.AuthMode)
	}
}

func TestAuthWarningBannerIsLoud(t *testing.T) {
	banner := authWarningBanner(BridgeConfig{Port: 8080, Instance: "default"})
	if !strings.Contains(banner, "NO AUTHENTICATION") || !strings.Contains(banner, "WHATSAPP_BRIDGE_TOKEN") {
		t.Fatalf("banner does not explain the risk: %s", banner)
	}
}

// --- BR-8: auth guard ----------------------------------------------------------

type guardResult struct {
	status int
	served bool
}

func runGuard(t *testing.T, cfg BridgeConfig, req *http.Request) (guardResult, []string) {
	t.Helper()
	var logs []string
	served := false
	handler := withAPIGuards(cfg, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		served = true
		w.WriteHeader(http.StatusOK)
	}), func(format string, args ...any) { logs = append(logs, fmt.Sprintf(format, args...)) })
	rec := httptest.NewRecorder()
	handler.ServeHTTP(rec, req)
	return guardResult{status: rec.Code, served: served}, logs
}

func newLocalRequest(method, path, token string) *http.Request {
	req := httptest.NewRequest(method, "http://127.0.0.1:8080"+path, nil)
	if token != "" {
		req.Header.Set("Authorization", "Bearer "+token)
	}
	return req
}

func TestGuardOffModeKeepsLegacyBehaviour(t *testing.T) {
	cfg := BridgeConfig{AuthMode: authModeOff, Instance: "default"}
	res, _ := runGuard(t, cfg, newLocalRequest(http.MethodPost, "/api/send", ""))
	if res.status != http.StatusOK || !res.served {
		t.Fatalf("off mode must serve unauthenticated calls, got %+v", res)
	}
}

func TestGuardEnforceRequiresBearerToken(t *testing.T) {
	cfg := BridgeConfig{AuthMode: authModeEnforce, Token: testToken, Instance: "t"}
	for name, token := range map[string]string{"missing": "", "wrong": otherTestToken, "prefix": testToken[:20]} {
		res, logs := runGuard(t, cfg, newLocalRequest(http.MethodPost, "/api/send", token))
		if res.status != http.StatusUnauthorized || res.served {
			t.Fatalf("%s token: got %+v, want 401 not served", name, res)
		}
		if len(logs) != 1 || strings.Contains(logs[0], testToken) {
			t.Fatalf("%s token: rejection must be logged without the token: %v", name, logs)
		}
	}
	res, _ := runGuard(t, cfg, newLocalRequest(http.MethodPost, "/api/send", testToken))
	if res.status != http.StatusOK || !res.served {
		t.Fatalf("valid token rejected: %+v", res)
	}
	lower := newLocalRequest(http.MethodPost, "/api/send", "")
	lower.Header.Set("Authorization", "bearer "+testToken)
	if res, _ := runGuard(t, cfg, lower); !res.served {
		t.Fatal("the bearer scheme must be case-insensitive")
	}
}

func TestGuardWarnModeLogsButServes(t *testing.T) {
	cfg := BridgeConfig{AuthMode: authModeWarn, Token: testToken, Instance: "t"}
	res, logs := runGuard(t, cfg, newLocalRequest(http.MethodPost, "/api/send", ""))
	if res.status != http.StatusOK || !res.served {
		t.Fatalf("warn mode must still serve: %+v", res)
	}
	if len(logs) != 1 || !strings.Contains(logs[0], "/api/send") {
		t.Fatalf("warn mode must log the unauthenticated call: %v", logs)
	}
	if _, logs := runGuard(t, cfg, newLocalRequest(http.MethodPost, "/api/send", testToken)); len(logs) != 0 {
		t.Fatalf("authenticated calls must not be logged: %v", logs)
	}
}

func TestGuardHealthIsExemptFromToken(t *testing.T) {
	cfg := BridgeConfig{AuthMode: authModeEnforce, Token: testToken, Instance: "t"}
	res, _ := runGuard(t, cfg, newLocalRequest(http.MethodGet, healthPath, ""))
	if !res.served {
		t.Fatalf("health must not need the token: %+v", res)
	}
}

func TestGuardRejectsBrowserAndRebindingRequests(t *testing.T) {
	for _, mode := range []string{authModeOff, authModeEnforce} {
		cfg := BridgeConfig{AuthMode: mode, Token: testToken, Instance: "t"}

		withOrigin := newLocalRequest(http.MethodPost, "/api/send", testToken)
		withOrigin.Header.Set("Origin", "https://attacker.example")
		if res, _ := runGuard(t, cfg, withOrigin); res.status != http.StatusForbidden || res.served {
			t.Fatalf("%s: browser request served: %+v", mode, res)
		}

		rebound := newLocalRequest(http.MethodGet, "/api/chats", testToken)
		rebound.Host = "attacker.example:8080"
		if res, _ := runGuard(t, cfg, rebound); res.status != http.StatusForbidden || res.served {
			t.Fatalf("%s: non-loopback Host served: %+v", mode, res)
		}
	}
	for _, host := range []string{"localhost:8080", "127.0.0.1:8081", "[::1]:8082", "localhost"} {
		if !isLoopbackHost(host) {
			t.Fatalf("%s must be accepted", host)
		}
	}
}

// --- BR-1 / BR-2 / BR-6: two instances side by side -----------------------------

func freePort(t *testing.T) int {
	t.Helper()
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer ln.Close()
	return ln.Addr().(*net.TCPAddr).Port
}

func apiCall(t *testing.T, method, url, token string) (int, map[string]any) {
	t.Helper()
	req, err := http.NewRequest(method, url, nil)
	if err != nil {
		t.Fatal(err)
	}
	if token != "" {
		req.Header.Set("Authorization", "Bearer "+token)
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	var body map[string]any
	_ = json.NewDecoder(resp.Body).Decode(&body)
	return resp.StatusCode, body
}

func TestTwoInstancesUseSeparatePortsStoresAndTokens(t *testing.T) {
	type instance struct {
		cfg   BridgeConfig
		store *MessageStore
		base  string
	}
	var instances []instance
	for i, token := range []string{testToken, otherTestToken} {
		dir := filepath.Join(t.TempDir(), fmt.Sprintf("store-%d", i))
		store, err := NewMessageStore(dir)
		if err != nil {
			t.Fatal(err)
		}
		defer store.Close()
		chat := fmt.Sprintf("12036300000000%d@g.us", i)
		if err := store.StoreChat(chat, fmt.Sprintf("chat %d", i), time.Now(), 0); err != nil {
			t.Fatal(err)
		}
		cfg := BridgeConfig{Port: freePort(t), StoreDir: dir, Instance: fmt.Sprintf("inst%d", i), Token: token, AuthMode: authModeEnforce}
		server, addr, err := startRESTServer(nil, store, cfg)
		if err != nil {
			t.Fatal(err)
		}
		defer server.Close()
		instances = append(instances, instance{cfg: cfg, store: store, base: "http://" + addr.String()})

		if _, err := os.Stat(filepath.Join(dir, "messages.db")); err != nil {
			t.Fatalf("messages.db not created in the configured store dir: %v", err)
		}
	}

	for i, inst := range instances {
		status, body := apiCall(t, http.MethodGet, inst.base+healthPath, "")
		if status != http.StatusOK || body["instance"] != inst.cfg.Instance || body["auth"] != authModeEnforce {
			t.Fatalf("instance %d health = %d %v", i, status, body)
		}

		other := instances[1-i]
		if status, _ := apiCall(t, http.MethodGet, inst.base+"/api/chats", other.cfg.Token); status != http.StatusUnauthorized {
			t.Fatalf("instance %d accepted the other instance's token (status %d)", i, status)
		}

		status, body = apiCall(t, http.MethodGet, inst.base+"/api/chats", inst.cfg.Token)
		if status != http.StatusOK {
			t.Fatalf("instance %d /api/chats = %d %v", i, status, body)
		}
		chats, _ := body["chats"].([]any)
		if len(chats) != 1 {
			t.Fatalf("instance %d must only see its own store, got %v", i, chats)
		}
		want := fmt.Sprintf("12036300000000%d@g.us", i)
		if got := chats[0].(map[string]any)["JID"]; got != want {
			t.Fatalf("instance %d served chat %v, want %s", i, got, want)
		}
	}

	// A third bridge on an occupied port must fail loudly instead of running
	// without a REST API.
	clash := instances[0].cfg
	clash.Instance = "clash"
	if server, _, err := startRESTServer(nil, instances[0].store, clash); err == nil {
		server.Close()
		t.Fatal("expected a bind error on an occupied port")
	}
}

// --- BR-4 / BR-6: schema migration ------------------------------------------------

// legacySchema is the schema written by bridges before this change (no
// unread tracking, no metadata columns, no message_events).
const legacySchema = `
	CREATE TABLE chats (jid TEXT PRIMARY KEY, name TEXT, last_message_time TIMESTAMP);
	CREATE TABLE messages (
		id TEXT, chat_jid TEXT, sender TEXT, content TEXT, timestamp TIMESTAMP,
		is_from_me BOOLEAN, media_type TEXT, filename TEXT, url TEXT,
		media_key BLOB, file_sha256 BLOB, file_enc_sha256 BLOB, file_length INTEGER,
		PRIMARY KEY (id, chat_jid), FOREIGN KEY (chat_jid) REFERENCES chats(jid)
	);
`

func TestMigrationUpgradesLegacyDatabaseInPlace(t *testing.T) {
	dir := t.TempDir()
	legacy, err := sql.Open("sqlite3", sqliteDSN(dir, "messages.db"))
	if err != nil {
		t.Fatal(err)
	}
	if _, err := legacy.Exec(legacySchema); err != nil {
		t.Fatal(err)
	}
	if _, err := legacy.Exec(`INSERT INTO chats (jid, name, last_message_time) VALUES (?, 'g', ?)`, testGroupJID, time.Now()); err != nil {
		t.Fatal(err)
	}
	if _, err := legacy.Exec(
		`INSERT INTO messages (id, chat_jid, sender, content, timestamp, is_from_me) VALUES ('OLD1', ?, ?, 'legacy text', ?, 0)`,
		testGroupJID, testLIDUser, time.Now(),
	); err != nil {
		t.Fatal(err)
	}
	legacy.Close()

	for run := 0; run < 2; run++ { // second run proves the migration is idempotent
		store, err := NewMessageStore(dir)
		if err != nil {
			t.Fatalf("run %d: %v", run, err)
		}
		for _, m := range messageColumnMigrations {
			has, err := tableHasColumn(store.db, m.table, m.column)
			if err != nil || !has {
				t.Fatalf("run %d: column %s.%s missing (err %v)", run, m.table, m.column, err)
			}
		}
		var content, sender string
		var senderJID sql.NullString
		if err := store.db.QueryRow(`SELECT content, sender, sender_jid FROM messages WHERE id = 'OLD1'`).Scan(&content, &sender, &senderJID); err != nil {
			t.Fatal(err)
		}
		if content != "legacy text" || sender != testLIDUser || senderJID.Valid {
			t.Fatalf("legacy row changed by migration: %q %q %v", content, sender, senderJID)
		}
		var n int
		if err := store.db.QueryRow(`SELECT COUNT(*) FROM message_events`).Scan(&n); err != nil || n != 0 {
			t.Fatalf("message_events not created: n=%d err=%v", n, err)
		}
		store.Close()
	}
}

// --- BR-4: trusted sender metadata -------------------------------------------------

func lidGroupMessage() (types.MessageInfo, *waProto.Message) {
	info := types.MessageInfo{
		MessageSource: types.MessageSource{
			Chat:      types.NewJID("120363000000000001", types.GroupServer),
			Sender:    types.JID{User: testLIDUser, Server: types.HiddenUserServer, Device: 12},
			SenderAlt: types.NewJID(testPhoneUser, types.DefaultUserServer),
			IsGroup:   true,
		},
		ID:        "MSGID00000000001",
		PushName:  "Display Name (unverified)",
		Timestamp: time.Date(2026, 9, 21, 10, 37, 2, 0, time.UTC),
	}
	msg := &waProto.Message{ExtendedTextMessage: &waProto.ExtendedTextMessage{
		Text: proto.String("@agent pode ajudar?"),
		ContextInfo: &waProto.ContextInfo{
			MentionedJID: []string{testMentionJID},
			StanzaID:     proto.String(testQuotedMsgID),
			IsForwarded:  proto.Bool(true),
		},
	}}
	return info, msg
}

func TestLIDGroupMessageStoresFullSenderMetadata(t *testing.T) {
	store, err := NewMessageStore(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	defer store.Close()
	info, msg := lidGroupMessage()
	if err := store.StoreChat(info.Chat.String(), "g", info.Timestamp, 0); err != nil {
		t.Fatal(err)
	}
	meta := extractMessageMeta(info, msg)
	if err := store.StoreMessage(info.ID, info.Chat.String(), info.Sender.User, extractTextContent(msg),
		info.Timestamp, false, "", "", "", nil, nil, nil, 0, meta); err != nil {
		t.Fatal(err)
	}

	var sender, senderJID, senderAlt, pushName, mentioned, quoted string
	var forwarded bool
	if err := store.db.QueryRow(
		`SELECT sender, sender_jid, sender_alt_jid, push_name, mentioned_jids, quoted_message_id, is_forwarded FROM messages WHERE id = ?`,
		info.ID,
	).Scan(&sender, &senderJID, &senderAlt, &pushName, &mentioned, &quoted, &forwarded); err != nil {
		t.Fatal(err)
	}
	if sender != testLIDUser {
		t.Fatalf("legacy sender column must keep the user part, got %q", sender)
	}
	if senderJID != testLIDUser+"@lid" {
		t.Fatalf("sender_jid = %q, want the full LID without device", senderJID)
	}
	if senderAlt != testPhoneUser+"@s.whatsapp.net" || pushName != info.PushName || quoted != testQuotedMsgID || !forwarded {
		t.Fatalf("metadata not stored: alt=%q push=%q quoted=%q fwd=%v", senderAlt, pushName, quoted, forwarded)
	}
	var mentions []string
	if err := json.Unmarshal([]byte(mentioned), &mentions); err != nil || len(mentions) != 1 || mentions[0] != testMentionJID {
		t.Fatalf("mentioned_jids = %q (%v)", mentioned, err)
	}

	// Reply context now resolves the quoted participant from sender_jid.
	reply, err := store.GetReplyContext(info.ID, info.Chat.String())
	if err != nil || reply.Sender != testLIDUser+"@lid" {
		t.Fatalf("reply sender = %q err=%v", reply.Sender, err)
	}

	// Re-storing without metadata (e.g. a sparse history-sync copy) must not
	// erase what the live event recorded.
	if err := store.StoreMessage(info.ID, info.Chat.String(), info.Sender.User, extractTextContent(msg),
		info.Timestamp, false, "", "", "", nil, nil, nil, 0, MessageMeta{}); err != nil {
		t.Fatal(err)
	}
	if err := store.db.QueryRow(`SELECT sender_jid, is_forwarded FROM messages WHERE id = ?`, info.ID).Scan(&senderJID, &forwarded); err != nil {
		t.Fatal(err)
	}
	if senderJID != testLIDUser+"@lid" || !forwarded {
		t.Fatalf("metadata erased by re-store: %q %v", senderJID, forwarded)
	}
}

func TestExtractMessageMetaWithoutContextInfo(t *testing.T) {
	info := types.MessageInfo{MessageSource: types.MessageSource{
		Chat:   types.NewJID(testPhoneUser, types.DefaultUserServer),
		Sender: types.NewJID(testPhoneUser, types.DefaultUserServer),
	}}
	meta := extractMessageMeta(info, &waProto.Message{Conversation: proto.String("oi")})
	if meta.SenderJID != testPhoneUser+"@s.whatsapp.net" || meta.SenderAltJID != "" || meta.mentionedJSON() != nil || meta.IsForwarded {
		t.Fatalf("unexpected meta: %+v", meta)
	}
}

// --- BR-5: edits and revocations are events ------------------------------------------

func protocolEvent(info types.MessageInfo, eventID string, after time.Duration, pm *waProto.ProtocolMessage) (MessageEvent, bool) {
	evInfo := info
	evInfo.ID = eventID
	evInfo.Timestamp = info.Timestamp.Add(after)
	return protocolMessageEvent(evInfo, &waProto.Message{ProtocolMessage: pm})
}

func TestEditAndRevokeAreLoggedWithoutOverwritingContent(t *testing.T) {
	store, err := NewMessageStore(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	defer store.Close()
	info, msg := lidGroupMessage()
	chat := info.Chat.String()
	if err := store.StoreChat(chat, "g", info.Timestamp, 0); err != nil {
		t.Fatal(err)
	}
	original := extractTextContent(msg)
	if err := store.StoreMessage(info.ID, chat, info.Sender.User, original, info.Timestamp, false,
		"", "", "", nil, nil, nil, 0, extractMessageMeta(info, msg)); err != nil {
		t.Fatal(err)
	}

	edit, ok := protocolEvent(info, "EDITEVENT0000001", time.Minute, &waProto.ProtocolMessage{
		Type:          waProto.ProtocolMessage_MESSAGE_EDIT.Enum(),
		Key:           &waProto.MessageKey{ID: proto.String(info.ID)},
		EditedMessage: &waProto.Message{Conversation: proto.String("texto editado")},
	})
	if !ok || edit.Type != messageEventEdit || edit.NewContent != "texto editado" || edit.TargetMessageID != info.ID {
		t.Fatalf("edit not recognised: %+v ok=%v", edit, ok)
	}
	for i := 0; i < 2; i++ { // idempotent on redelivery
		if err := store.RecordMessageEvent(edit); err != nil {
			t.Fatal(err)
		}
	}

	// A later re-store of the same message must not replace the original text.
	if err := store.StoreMessage(info.ID, chat, info.Sender.User, "texto editado", info.Timestamp, false,
		"", "", "", nil, nil, nil, 0, MessageMeta{}); err != nil {
		t.Fatal(err)
	}

	revoke, ok := protocolEvent(info, "REVOKEEVENT00001", 2*time.Minute, &waProto.ProtocolMessage{
		Type: waProto.ProtocolMessage_REVOKE.Enum(),
		Key:  &waProto.MessageKey{ID: proto.String(info.ID)},
	})
	if !ok || revoke.Type != messageEventRevoke {
		t.Fatalf("revoke not recognised: %+v", revoke)
	}
	if err := store.RecordMessageEvent(revoke); err != nil {
		t.Fatal(err)
	}

	var content string
	var editedAt, revokedAt sql.NullString
	if err := store.db.QueryRow(`SELECT content, edited_at, revoked_at FROM messages WHERE id = ?`, info.ID).
		Scan(&content, &editedAt, &revokedAt); err != nil {
		t.Fatal(err)
	}
	if content != original {
		t.Fatalf("original content overwritten: %q", content)
	}
	if !editedAt.Valid || !revokedAt.Valid {
		t.Fatalf("edited_at/revoked_at not flagged: %v %v", editedAt, revokedAt)
	}

	rows, err := store.db.Query(`SELECT event_type, COALESCE(new_content, ''), sender_jid FROM message_events WHERE target_message_id = ? ORDER BY timestamp`, info.ID)
	if err != nil {
		t.Fatal(err)
	}
	defer rows.Close()
	var got []string
	for rows.Next() {
		var typ, newContent, sender string
		if err := rows.Scan(&typ, &newContent, &sender); err != nil {
			t.Fatal(err)
		}
		if sender != testLIDUser+"@lid" {
			t.Fatalf("event sender_jid = %q", sender)
		}
		got = append(got, typ+":"+newContent)
	}
	if strings.Join(got, ",") != "edit:texto editado,revoke:" {
		t.Fatalf("events = %v", got)
	}
}

func TestNonEditProtocolMessagesAreNotEvents(t *testing.T) {
	info, _ := lidGroupMessage()
	if _, ok := protocolMessageEvent(info, &waProto.Message{Conversation: proto.String("oi")}); ok {
		t.Fatal("plain text must not be an event")
	}
	if _, ok := protocolMessageEvent(info, &waProto.Message{ProtocolMessage: &waProto.ProtocolMessage{
		Type: waProto.ProtocolMessage_EPHEMERAL_SETTING.Enum(),
		Key:  &waProto.MessageKey{ID: proto.String("X")},
	}}); ok {
		t.Fatal("ephemeral setting must not be an event")
	}
}

// --- BR-7: send response carries the message id -----------------------------------------

func TestSendResponseIncludesMessageIDAndTimestamp(t *testing.T) {
	ts := time.Date(2026, 9, 27, 12, 0, 0, 0, time.UTC)
	ok := sendResultResponse(true, "Message sent", whatsmeow.SendResponse{ID: "3EB0AAAAAAAAAAAA", Timestamp: ts})
	data, _ := json.Marshal(ok)
	var body map[string]any
	_ = json.Unmarshal(data, &body)
	if body["message_id"] != "3EB0AAAAAAAAAAAA" || body["timestamp"] != "2026-09-27T12:00:00Z" || body["success"] != true {
		t.Fatalf("send response = %s", data)
	}

	failed := sendResultResponse(false, "Error sending message", whatsmeow.SendResponse{ID: "IGNORED"})
	data, _ = json.Marshal(failed)
	if strings.Contains(string(data), "message_id") || strings.Contains(string(data), "timestamp") {
		t.Fatalf("failed send must not carry an id: %s", data)
	}
}

// Guard against a regression where handlers are registered on the global
// DefaultServeMux (which would make two instances in one process collide).
func TestRESTHandlersAreNotGlobal(t *testing.T) {
	store, err := NewMessageStore(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	defer store.Close()
	_ = newRESTHandler(nil, store, BridgeConfig{Instance: "a", AuthMode: authModeOff})
	_ = newRESTHandler(nil, store, BridgeConfig{Instance: "b", AuthMode: authModeOff}) // panics on duplicate global registration
}

// --- Review fixes (Codex cross-review of PR #4) ----------------------------------------

func storePlain(t *testing.T, store *MessageStore, id, chat, content string, ts time.Time) {
	t.Helper()
	if err := store.StoreChat(chat, "c", ts, 0); err != nil {
		t.Fatal(err)
	}
	if err := store.StoreMessage(id, chat, "5547999990000", content, ts, false,
		"", "", "", nil, nil, nil, 0, MessageMeta{}); err != nil {
		t.Fatal(err)
	}
}

func flags(t *testing.T, store *MessageStore, id, chat string) (edited, revoked sql.NullString) {
	t.Helper()
	if err := store.db.QueryRow(`SELECT edited_at, revoked_at FROM messages WHERE id = ? AND chat_jid = ?`, id, chat).
		Scan(&edited, &revoked); err != nil {
		t.Fatal(err)
	}
	return edited, revoked
}

func TestEventNeverFlagsMessageInUnrelatedChat(t *testing.T) {
	store, err := NewMessageStore(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	defer store.Close()
	ts := time.Date(2026, 10, 8, 12, 0, 0, 0, time.UTC)
	groupA, groupB := "120363000000000001@g.us", "120363000000000002@g.us"
	storePlain(t, store, "SAMEID0000000001", groupA, "oi", ts)

	if err := store.RecordMessageEvent(MessageEvent{
		EventID: "EV1", ChatJID: groupB, TargetMessageID: "SAMEID0000000001",
		Type: messageEventRevoke, Timestamp: ts.Add(time.Minute),
	}); err != nil {
		t.Fatal(err)
	}
	if _, revoked := flags(t, store, "SAMEID0000000001", groupA); revoked.Valid {
		t.Fatalf("revoke in group B flagged the message in group A: %v", revoked)
	}
}

func TestEventFlagsVerifiedAliasChat(t *testing.T) {
	store, err := NewMessageStore(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	defer store.Close()
	ts := time.Date(2026, 10, 8, 12, 0, 0, 0, time.UTC)
	lidChat, pnChat := testLIDUser+"@lid", testPhoneUser+"@s.whatsapp.net"
	storePlain(t, store, "ALIASID000000001", lidChat, "oi", ts)

	if err := store.RecordMessageEvent(MessageEvent{
		EventID: "EV2", ChatJID: pnChat, AliasChatJIDs: []string{lidChat}, TargetMessageID: "ALIASID000000001",
		Type: messageEventEdit, NewContent: "oi!", Timestamp: ts.Add(time.Minute),
	}); err != nil {
		t.Fatal(err)
	}
	if edited, _ := flags(t, store, "ALIASID000000001", lidChat); !edited.Valid {
		t.Fatal("edit under the verified PN alias did not flag the LID-stored message")
	}
}

func TestPendingEventsApplyWhenTargetArrives(t *testing.T) {
	store, err := NewMessageStore(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	defer store.Close()
	ts := time.Date(2026, 10, 8, 12, 0, 0, 0, time.UTC)
	chat := "120363000000000003@g.us"
	for _, ev := range []MessageEvent{
		{EventID: "EV3", ChatJID: chat, TargetMessageID: "LATEID0000000001", Type: messageEventEdit, NewContent: "novo", Timestamp: ts.Add(time.Minute)},
		{EventID: "EV4", ChatJID: chat, TargetMessageID: "LATEID0000000001", Type: messageEventRevoke, Timestamp: ts.Add(2 * time.Minute)},
	} {
		if err := store.RecordMessageEvent(ev); err != nil {
			t.Fatal(err)
		}
	}
	storePlain(t, store, "LATEID0000000001", chat, "original", ts)

	edited, revoked := flags(t, store, "LATEID0000000001", chat)
	if !edited.Valid || !revoked.Valid {
		t.Fatalf("pending events not applied on insert: edited=%v revoked=%v", edited, revoked)
	}
	var content string
	if err := store.db.QueryRow(`SELECT content FROM messages WHERE id = ?`, "LATEID0000000001").Scan(&content); err != nil {
		t.Fatal(err)
	}
	if content != "original" {
		t.Fatalf("content = %q, want the original", content)
	}
	// An unrelated message in the same chat stays unflagged.
	storePlain(t, store, "OTHERID000000001", chat, "outra", ts)
	if e, r := flags(t, store, "OTHERID000000001", chat); e.Valid || r.Valid {
		t.Fatalf("unrelated message flagged: %v %v", e, r)
	}
}

func TestStoreDirWithURICharactersStaysIsolated(t *testing.T) {
	base := t.TempDir()
	ts := time.Date(2026, 10, 8, 12, 0, 0, 0, time.UTC)
	dirs := []string{"sales%31", "sales1", "a?b#c"}
	for i, name := range dirs {
		store, err := NewMessageStore(filepath.Join(base, name))
		if err != nil {
			t.Fatalf("%s: %v", name, err)
		}
		storePlain(t, store, fmt.Sprintf("ISOID%011d", i), fmt.Sprintf("12036300000000001%d@g.us", i), name, ts)
		store.Close()
	}
	for i, name := range dirs {
		if _, err := os.Stat(filepath.Join(base, name, "messages.db")); err != nil {
			t.Fatalf("messages.db not created under the literal directory %q: %v", name, err)
		}
		store, err := NewMessageStore(filepath.Join(base, name))
		if err != nil {
			t.Fatal(err)
		}
		var n int
		if err := store.db.QueryRow(`SELECT COUNT(*) FROM messages`).Scan(&n); err != nil {
			t.Fatal(err)
		}
		var content string
		_ = store.db.QueryRow(`SELECT content FROM messages`).Scan(&content)
		store.Close()
		if n != 1 || content != name {
			t.Fatalf("store %q (#%d) sees %d messages (%q): instances are not isolated", name, i, n, content)
		}
	}
}
