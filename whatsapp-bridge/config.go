package main

import (
	"crypto/sha256"
	"crypto/subtle"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"net"
	"net/http"
	"os"
	"strconv"
	"strings"
)

// Defaults keep a bridge started without any flag or env var behaving exactly
// like the historical build: port 8080, store/ relative to the working
// directory, no REST authentication (with a loud warning).
const (
	defaultBridgePort = 8080
	defaultStoreDir   = "store"
	defaultInstance   = "default"
	// maxFullHistoryDays caps the full-sync request (WhatsApp itself limits what
	// the phone actually sends).
	maxFullHistoryDays = 3650

	// minTokenLength rejects trivially guessable tokens. 32 random bytes in
	// base64url (python3 -c 'import secrets;print(secrets.token_urlsafe(32))')
	// produce 43 characters.
	minTokenLength = 16

	authModeOff     = "off"
	authModeWarn    = "warn"
	authModeEnforce = "enforce"

	healthPath = "/api/health"
)

// BridgeConfig holds everything that used to be hard-coded in main.go and
// that must differ between bridge instances running side by side (one per
// WhatsApp number).
type BridgeConfig struct {
	Port     int
	StoreDir string
	Instance string

	// Token is the shared secret required as "Authorization: Bearer <token>"
	// on every REST call (except the health probe). It is only accepted from
	// the environment or a file, never from a flag, so it never shows up in
	// the process list.
	Token string
	// AuthMode is "off" (no token configured), "warn" (token configured,
	// unauthenticated calls are logged but still served — migration aid) or
	// "enforce" (unauthenticated calls get HTTP 401).
	AuthMode string

	// FullHistoryDays > 0 asks the phone for a full history sync of up to that
	// many days when a NEW device is paired (RequireFullSync + FullSyncDaysLimit
	// in the companion DeviceProps). It has no effect on an already paired
	// session. 0 keeps WhatsApp's default (recent history only).
	FullHistoryDays uint32
}

// loadBridgeConfig resolves the configuration with precedence
// flag > environment > default.
//
// Flags:   -port, -store-dir, -instance, -require-token, -full-history-days
// Env:     WHATSAPP_BRIDGE_PORT, WHATSAPP_STORE_DIR, WHATSAPP_BRIDGE_INSTANCE,
//
//	WHATSAPP_BRIDGE_TOKEN or WHATSAPP_BRIDGE_TOKEN_FILE,
//	WHATSAPP_BRIDGE_AUTH_MODE (warn|enforce), WHATSAPP_BRIDGE_REQUIRE_TOKEN,
//	WHATSAPP_FULL_HISTORY_DAYS
func loadBridgeConfig(args []string, getenv func(string) string) (BridgeConfig, error) {
	cfg := BridgeConfig{Port: defaultBridgePort, StoreDir: defaultStoreDir, Instance: defaultInstance}

	fs := flag.NewFlagSet("whatsapp-bridge", flag.ContinueOnError)
	fs.SetOutput(io.Discard)
	portFlag := fs.Int("port", 0, "REST API port on 127.0.0.1 (env WHATSAPP_BRIDGE_PORT, default 8080)")
	storeFlag := fs.String("store-dir", "", "directory for messages.db, whatsapp.db and the media cache (env WHATSAPP_STORE_DIR, default ./store)")
	instanceFlag := fs.String("instance", "", "instance name used in logs and /api/health (env WHATSAPP_BRIDGE_INSTANCE)")
	requireTokenFlag := fs.Bool("require-token", false, "refuse to start without a REST token (env WHATSAPP_BRIDGE_REQUIRE_TOKEN)")
	fullHistoryFlag := fs.Uint("full-history-days", 0, "on a new pairing, request a full history sync of up to N days (env WHATSAPP_FULL_HISTORY_DAYS, default 0 = WhatsApp default)")
	if err := fs.Parse(args); err != nil {
		if errors.Is(err, flag.ErrHelp) {
			fs.SetOutput(os.Stderr)
			fs.PrintDefaults()
			return cfg, err
		}
		return cfg, fmt.Errorf("invalid flags: %w", err)
	}
	flagSet := map[string]bool{}
	fs.Visit(func(f *flag.Flag) { flagSet[f.Name] = true })

	if flagSet["port"] {
		cfg.Port = *portFlag
	} else if raw := strings.TrimSpace(getenv("WHATSAPP_BRIDGE_PORT")); raw != "" {
		port, err := strconv.Atoi(raw)
		if err != nil {
			return cfg, fmt.Errorf("WHATSAPP_BRIDGE_PORT=%q is not a number", raw)
		}
		cfg.Port = port
	}
	if cfg.Port < 1 || cfg.Port > 65535 {
		return cfg, fmt.Errorf("port %d out of range 1-65535", cfg.Port)
	}

	if flagSet["store-dir"] {
		cfg.StoreDir = strings.TrimSpace(*storeFlag)
	} else if raw := strings.TrimSpace(getenv("WHATSAPP_STORE_DIR")); raw != "" {
		cfg.StoreDir = raw
	}
	if cfg.StoreDir == "" {
		return cfg, errors.New("store dir must not be empty")
	}

	if flagSet["instance"] {
		cfg.Instance = strings.TrimSpace(*instanceFlag)
	} else if raw := strings.TrimSpace(getenv("WHATSAPP_BRIDGE_INSTANCE")); raw != "" {
		cfg.Instance = raw
	}
	if cfg.Instance == "" {
		cfg.Instance = defaultInstance
	}

	if flagSet["full-history-days"] {
		cfg.FullHistoryDays = uint32(*fullHistoryFlag)
	} else if raw := strings.TrimSpace(getenv("WHATSAPP_FULL_HISTORY_DAYS")); raw != "" {
		days, err := strconv.ParseUint(raw, 10, 32)
		if err != nil {
			return cfg, fmt.Errorf("WHATSAPP_FULL_HISTORY_DAYS=%q is not a non-negative number", raw)
		}
		cfg.FullHistoryDays = uint32(days)
	}
	if cfg.FullHistoryDays > maxFullHistoryDays {
		return cfg, fmt.Errorf("full history days %d above the maximum %d", cfg.FullHistoryDays, maxFullHistoryDays)
	}

	requireToken := *requireTokenFlag
	if !flagSet["require-token"] {
		if raw := strings.TrimSpace(getenv("WHATSAPP_BRIDGE_REQUIRE_TOKEN")); raw != "" {
			value, err := strconv.ParseBool(raw)
			if err != nil {
				return cfg, fmt.Errorf("WHATSAPP_BRIDGE_REQUIRE_TOKEN=%q is not a boolean", raw)
			}
			requireToken = value
		}
	}

	token, err := resolveToken(getenv)
	if err != nil {
		return cfg, err
	}
	cfg.Token = token
	if token != "" && len(token) < minTokenLength {
		return cfg, fmt.Errorf("REST token is too short (%d chars, minimum %d)", len(token), minTokenLength)
	}
	if requireToken && token == "" {
		return cfg, errors.New("a REST token is required (require-token) but WHATSAPP_BRIDGE_TOKEN / WHATSAPP_BRIDGE_TOKEN_FILE is not set")
	}

	mode := strings.ToLower(strings.TrimSpace(getenv("WHATSAPP_BRIDGE_AUTH_MODE")))
	switch {
	case mode == "" && token == "":
		cfg.AuthMode = authModeOff
	case mode == "" || mode == authModeEnforce:
		if token == "" {
			return cfg, errors.New("WHATSAPP_BRIDGE_AUTH_MODE=enforce needs WHATSAPP_BRIDGE_TOKEN or WHATSAPP_BRIDGE_TOKEN_FILE")
		}
		cfg.AuthMode = authModeEnforce
	case mode == authModeWarn:
		if token == "" {
			return cfg, errors.New("WHATSAPP_BRIDGE_AUTH_MODE=warn needs WHATSAPP_BRIDGE_TOKEN or WHATSAPP_BRIDGE_TOKEN_FILE")
		}
		cfg.AuthMode = authModeWarn
	default:
		return cfg, fmt.Errorf("WHATSAPP_BRIDGE_AUTH_MODE=%q is invalid (use warn or enforce; leave unset to derive it from the token)", mode)
	}

	return cfg, nil
}

func resolveToken(getenv func(string) string) (string, error) {
	token := strings.TrimSpace(getenv("WHATSAPP_BRIDGE_TOKEN"))
	tokenFile := strings.TrimSpace(getenv("WHATSAPP_BRIDGE_TOKEN_FILE"))
	if token != "" && tokenFile != "" {
		return "", errors.New("set only one of WHATSAPP_BRIDGE_TOKEN and WHATSAPP_BRIDGE_TOKEN_FILE")
	}
	if tokenFile == "" {
		return token, nil
	}
	data, err := os.ReadFile(tokenFile)
	if err != nil {
		return "", fmt.Errorf("read WHATSAPP_BRIDGE_TOKEN_FILE: %w", err)
	}
	token = strings.TrimSpace(string(data))
	if token == "" {
		return "", errors.New("WHATSAPP_BRIDGE_TOKEN_FILE is empty")
	}
	return token, nil
}

// authWarningBanner is printed at startup when the REST API runs without a
// token, so the insecure state is impossible to miss in the journal.
func authWarningBanner(cfg BridgeConfig) string {
	line := strings.Repeat("!", 78)
	return fmt.Sprintf(`%s
!! WARNING [instance %s]: the REST API on 127.0.0.1:%d has NO AUTHENTICATION.
!! Any local process (and any other agent/profile running as this user) can
!! send WhatsApp messages through this number. Set WHATSAPP_BRIDGE_TOKEN (or
!! WHATSAPP_BRIDGE_TOKEN_FILE) for the bridge AND for its clients.
!! See README "REST API authentication" for the zero-downtime migration.
%s`, line, cfg.Instance, cfg.Port, line)
}

// isLoopbackHost accepts only Host headers that name the loopback interface.
// This blocks DNS-rebinding attacks from a browser against the local API.
func isLoopbackHost(hostport string) bool {
	host := hostport
	if h, _, err := net.SplitHostPort(hostport); err == nil {
		host = h
	}
	host = strings.Trim(strings.ToLower(host), "[]")
	switch host {
	case "localhost", "127.0.0.1", "::1":
		return true
	}
	return false
}

func bearerToken(r *http.Request) string {
	header := strings.TrimSpace(r.Header.Get("Authorization"))
	if len(header) < 7 || !strings.EqualFold(header[:7], "bearer ") {
		return ""
	}
	return strings.TrimSpace(header[7:])
}

// tokensEqual compares in constant time, independent of the token lengths.
func tokensEqual(got, want string) bool {
	a := sha256.Sum256([]byte(got))
	b := sha256.Sum256([]byte(want))
	return subtle.ConstantTimeCompare(a[:], b[:]) == 1
}

func writeAPIError(w http.ResponseWriter, status int, message string) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(map[string]any{"success": false, "message": message})
}

// withAPIGuards wraps the REST mux with:
//   - a loopback Host check (anti DNS rebinding),
//   - a browser check (requests carrying an Origin header are refused: no
//     legitimate client of this API is a web page, and this closes the
//     cross-site POST hole even when no token is configured),
//   - bearer-token authentication according to cfg.AuthMode.
//
// The health probe is exempt from the token so supervisors can check the
// instance without holding the secret; it exposes no chat data.
func withAPIGuards(cfg BridgeConfig, next http.Handler, logf func(format string, args ...any)) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if !isLoopbackHost(r.Host) {
			writeAPIError(w, http.StatusForbidden, "forbidden host")
			return
		}
		if r.Header.Get("Origin") != "" {
			writeAPIError(w, http.StatusForbidden, "browser requests are not allowed")
			return
		}
		if r.URL.Path == healthPath || cfg.AuthMode == authModeOff {
			next.ServeHTTP(w, r)
			return
		}
		if tokensEqual(bearerToken(r), cfg.Token) {
			next.ServeHTTP(w, r)
			return
		}
		if cfg.AuthMode == authModeWarn {
			logf("[AUTH][%s] unauthenticated %s %s allowed (WHATSAPP_BRIDGE_AUTH_MODE=warn); this will be rejected under enforce",
				cfg.Instance, r.Method, r.URL.Path)
			next.ServeHTTP(w, r)
			return
		}
		logf("[AUTH][%s] rejected unauthenticated %s %s", cfg.Instance, r.Method, r.URL.Path)
		w.Header().Set("WWW-Authenticate", `Bearer realm="whatsapp-bridge"`)
		writeAPIError(w, http.StatusUnauthorized, "unauthorized: missing or invalid bearer token")
	})
}
