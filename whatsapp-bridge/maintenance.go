package main

import (
	"context"
	"errors"
	"os"
	"path/filepath"

	"go.mau.fi/whatsmeow/store/sqlstore"
)

// checkStoreCopy cannot start any client, HTTP listener, QR or event loop.
// The executor must create this marker only in a private snapshot directory.
func checkStoreCopy(dir string) error {
	marker, err := os.Lstat(filepath.Join(dir, ".maintenance-canary"))
	if err != nil || !marker.Mode().IsRegular() {
		return errors.New("a disposable copy marked .maintenance-canary is required")
	}
	container, err := sqlstore.New(context.Background(), "sqlite3", sqliteDSN(dir, "whatsapp.db"), nil)
	if err != nil {
		return err
	}
	defer container.Close()
	messages, err := NewMessageStore(dir)
	if err != nil {
		return err
	}
	return messages.Close()
}
