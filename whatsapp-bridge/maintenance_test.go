package main

import (
	"os"
	"path/filepath"
	"testing"
)

func TestStoreCanaryRequiresDisposableMarker(t *testing.T) {
	dir := t.TempDir()
	if err := checkStoreCopy(dir); err == nil {
		t.Fatal("unmarked store must be rejected")
	}
	if _, err := os.Stat(filepath.Join(dir, "whatsapp.db")); !os.IsNotExist(err) {
		t.Fatal("unmarked store must not be touched")
	}
	if err := os.WriteFile(filepath.Join(dir, ".maintenance-canary"), nil, 0600); err != nil {
		t.Fatal(err)
	}
	if err := checkStoreCopy(dir); err != nil {
		t.Fatal(err)
	}
}
