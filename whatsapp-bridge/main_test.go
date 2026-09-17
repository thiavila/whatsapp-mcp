package main

import (
	"database/sql"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"testing"
	"time"
)

func TestParseRecipientJIDCanonicalizesPhoneNumber(t *testing.T) {
	jid, err := parseRecipientJID("559881212573")
	if err != nil {
		t.Fatal(err)
	}
	if got, want := jid.String(), "559881212573@s.whatsapp.net"; got != want {
		t.Fatalf("recipient JID = %q, want %q", got, want)
	}
}

func TestReplyMetadataCannotBeInjectedThroughJSON(t *testing.T) {
	var req SendMessageRequest
	err := json.Unmarshal([]byte(`{
		"recipient":"120363430688593294@g.us",
		"message":"hello",
		"reply_to_message_id":"ABC",
		"reply_to_sender":"forged@lid",
		"reply_to_media_type":"image",
		"reply_to_content":"forged"
	}`), &req)
	if err != nil {
		t.Fatal(err)
	}
	if req.ReplyToMessageID != "ABC" {
		t.Fatalf("reply id = %q, want ABC", req.ReplyToMessageID)
	}
	if req.ReplyToSender != "" || req.ReplyToMediaType != "" || req.ReplyToContent != "" {
		t.Fatal("reply metadata must come from the message store, not client JSON")
	}
}

func TestGetReplyContextIsScopedToRecipientChat(t *testing.T) {
	db, err := sql.Open("sqlite3", filepath.Join(t.TempDir(), "messages.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	_, err = db.Exec(`CREATE TABLE messages (
		id TEXT, chat_jid TEXT, sender TEXT, content TEXT,
		media_type TEXT, filename TEXT,
		PRIMARY KEY (id, chat_jid)
	)`)
	if err != nil {
		t.Fatal(err)
	}
	_, err = db.Exec(
		"INSERT INTO messages (id, chat_jid, sender, content, media_type, filename) VALUES (?, ?, ?, ?, ?, ?)",
		"ABC", "target@g.us", "141536503300224", "", "image", "photo.jpg",
	)
	if err != nil {
		t.Fatal(err)
	}
	store := &MessageStore{db: db}

	context, err := store.GetReplyContext("ABC", "target@g.us")
	if err != nil {
		t.Fatal(err)
	}
	if context.Sender != "141536503300224" || context.MediaType != "image" || context.Content != "" {
		t.Fatalf("unexpected reply context: %+v", context)
	}
	if _, err := store.GetReplyContext("ABC", "other@g.us"); !errors.Is(err, sql.ErrNoRows) {
		t.Fatalf("wrong-chat lookup error = %v, want sql.ErrNoRows", err)
	}
}

func TestBuildOutgoingMessageQuotesImage(t *testing.T) {
	req := SendMessageRequest{
		Message:          "corrigimos o problema",
		ReplyToMessageID: "AC668AAC2DF5547FD6A833FBC2147B16",
		ReplyToSender:    "141536503300224@lid",
		ReplyToMediaType: "image",
	}

	msg := buildOutgoingMessage(req)
	extended := msg.GetExtendedTextMessage()
	if extended == nil {
		t.Fatal("expected an extended text message for a quoted reply")
	}
	if got, want := extended.GetText(), req.Message; got != want {
		t.Fatalf("text = %q, want %q", got, want)
	}
	context := extended.GetContextInfo()
	if context == nil {
		t.Fatal("expected quoted reply context")
	}
	if got, want := context.GetStanzaID(), req.ReplyToMessageID; got != want {
		t.Fatalf("stanza id = %q, want %q", got, want)
	}
	if got, want := context.GetParticipant(), req.ReplyToSender; got != want {
		t.Fatalf("participant = %q, want %q", got, want)
	}
	if context.GetQuotedMessage().GetImageMessage() == nil {
		t.Fatal("expected quoted image metadata")
	}
}

func TestExtractDirectPathFromURLPreservesWhatsAppQuery(t *testing.T) {
	input := "https://mmg.whatsapp.net/v/t62.7161-24/video.enc?ccb=11-4&oh=token&oe=expiry&mms3=true"
	want := "/v/t62.7161-24/video.enc?ccb=11-4&oh=token&oe=expiry&mms3=true"

	if got := extractDirectPathFromURL(input); got != want {
		t.Fatalf("extractDirectPathFromURL() = %q, want %q", got, want)
	}
}

func TestUniqueMediaFilenameRemovesPathTraversal(t *testing.T) {
	if got, want := uniqueMediaFilename("ABC123", "../../.ssh/authorized_keys"), "ABC123-authorized_keys"; got != want {
		t.Fatalf("uniqueMediaFilename() = %q, want %q", got, want)
	}
	if got, want := uniqueMediaFilename("ABC123", `..\..\secret.txt`), "ABC123-secret.txt"; got != want {
		t.Fatalf("uniqueMediaFilename() = %q, want %q", got, want)
	}
}

func TestSanitizeMediaFilenamePreservesLegacyCacheName(t *testing.T) {
	if got, want := sanitizeMediaFilename("video_20260910_104527.mp4"), "video_20260910_104527.mp4"; got != want {
		t.Fatalf("sanitizeMediaFilename() = %q, want %q", got, want)
	}
}

func TestChatMediaDirectoryDoesNotEscapeStore(t *testing.T) {
	if got, want := chatMediaDirectory("store", "../../outside:1"), filepath.Join("store", "outside_1"); got != want {
		t.Fatalf("chatMediaDirectory() = %q, want %q", got, want)
	}
	if got, want := chatMediaDirectory("store", ".."), filepath.Join("store", "media"); got != want {
		t.Fatalf("chatMediaDirectory() = %q, want %q", got, want)
	}
}

func TestSizeLimitedFileRejectsWritesBeyondLimit(t *testing.T) {
	raw, err := os.CreateTemp(t.TempDir(), "media-*")
	if err != nil {
		t.Fatal(err)
	}
	defer raw.Close()

	limited := &sizeLimitedFile{File: raw, maxBytes: 4}
	if _, err := limited.Write([]byte("1234")); err != nil {
		t.Fatal(err)
	}
	if _, err := limited.Write([]byte("5")); !errors.Is(err, errMediaTooLarge) {
		t.Fatalf("overflow error = %v, want %v", err, errMediaTooLarge)
	}
}

func TestMediaCacheHasCapacityCountsOnlyChatMedia(t *testing.T) {
	root := t.TempDir()
	if err := os.WriteFile(filepath.Join(root, "messages.db"), make([]byte, 100), 0o600); err != nil {
		t.Fatal(err)
	}
	chatDir := filepath.Join(root, "123@g.us")
	if err := os.Mkdir(chatDir, 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(chatDir, "video.mp4"), make([]byte, 60), 0o600); err != nil {
		t.Fatal(err)
	}

	ok, err := mediaCacheHasCapacity(root, 40, 100)
	if err != nil {
		t.Fatal(err)
	}
	if !ok {
		t.Fatal("expected media cache to have capacity at the exact limit")
	}
	ok, err = mediaCacheHasCapacity(root, 41, 100)
	if err != nil {
		t.Fatal(err)
	}
	if ok {
		t.Fatal("expected media cache capacity check to reject overflow")
	}
}

func TestMediaCacheReservationsPreventConcurrentOverflow(t *testing.T) {
	root := t.TempDir()
	chatDir := filepath.Join(root, "123@g.us")
	if err := os.Mkdir(chatDir, 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(chatDir, "existing.mp4"), make([]byte, 60), 0o600); err != nil {
		t.Fatal(err)
	}

	reservations := &mediaCacheReservations{}
	releaseFirst, ok, err := reservations.reserve(root, 30, 100)
	if err != nil || !ok {
		t.Fatalf("first reservation: ok=%v err=%v", ok, err)
	}
	if _, ok, err := reservations.reserve(root, 20, 100); err != nil || ok {
		t.Fatalf("overlapping reservation: ok=%v err=%v", ok, err)
	}
	releaseFirst()
	releaseSecond, ok, err := reservations.reserve(root, 20, 100)
	if err != nil || !ok {
		t.Fatalf("reservation after release: ok=%v err=%v", ok, err)
	}
	releaseSecond()
}

func TestEagerMediaDownloadEnabled(t *testing.T) {
	t.Setenv("WHATSAPP_EAGER_MEDIA_DOWNLOAD", "true")
	if !eagerMediaDownloadEnabled() {
		t.Fatal("expected eager media download to be enabled")
	}

	t.Setenv("WHATSAPP_EAGER_MEDIA_DOWNLOAD", "false")
	if eagerMediaDownloadEnabled() {
		t.Fatal("expected eager media download to be disabled")
	}
}

func TestScheduleEagerMediaDownloadCachesMedia(t *testing.T) {
	downloaded := make(chan struct{}, 1)
	slots := make(chan struct{}, 1)
	if !scheduleEagerMediaDownload(
		true,
		"video",
		1024,
		2048,
		slots,
		func() error {
			downloaded <- struct{}{}
			return nil
		},
		nil,
	) {
		t.Fatal("expected eager media download to be scheduled")
	}

	select {
	case <-downloaded:
	case <-time.After(time.Second):
		t.Fatal("timed out waiting for eager media download")
	}
}

func TestScheduleEagerMediaDownloadSkipsWhenDisabled(t *testing.T) {
	downloaded := make(chan struct{}, 1)
	slots := make(chan struct{}, 1)
	if scheduleEagerMediaDownload(
		false,
		"video",
		1024,
		2048,
		slots,
		func() error {
			downloaded <- struct{}{}
			return nil
		},
		nil,
	) {
		t.Fatal("download was scheduled while eager media download was disabled")
	}

	select {
	case <-downloaded:
		t.Fatal("download ran while eager media download was disabled")
	case <-time.After(50 * time.Millisecond):
	}
}

func TestScheduleEagerMediaDownloadReportsFailure(t *testing.T) {
	want := errors.New("download failed")
	reported := make(chan error, 1)
	slots := make(chan struct{}, 1)
	if !scheduleEagerMediaDownload(
		true,
		"image",
		1024,
		2048,
		slots,
		func() error { return want },
		func(err error) { reported <- err },
	) {
		t.Fatal("expected eager media download to be scheduled")
	}

	select {
	case got := <-reported:
		if !errors.Is(got, want) {
			t.Fatalf("reported error = %v, want %v", got, want)
		}
	case <-time.After(time.Second):
		t.Fatal("timed out waiting for eager media download error")
	}
}

func TestScheduleEagerMediaDownloadRejectsOversizedMedia(t *testing.T) {
	downloaded := make(chan struct{}, 1)
	slots := make(chan struct{}, 1)
	if scheduleEagerMediaDownload(
		true,
		"document",
		2049,
		2048,
		slots,
		func() error {
			downloaded <- struct{}{}
			return nil
		},
		nil,
	) {
		t.Fatal("oversized media was scheduled")
	}
}

func TestScheduleEagerMediaDownloadRejectsWhenWorkersAreBusy(t *testing.T) {
	downloaded := make(chan struct{}, 1)
	slots := make(chan struct{}, 1)
	slots <- struct{}{}
	if scheduleEagerMediaDownload(
		true,
		"audio",
		1024,
		2048,
		slots,
		func() error {
			downloaded <- struct{}{}
			return nil
		},
		nil,
	) {
		t.Fatal("media was scheduled while all workers were busy")
	}
}
