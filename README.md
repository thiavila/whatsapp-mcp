# WhatsApp MCP

An unofficial, local-first [Model Context Protocol (MCP)](https://modelcontextprotocol.io/) server that lets compatible AI clients search, read, send, and understand WhatsApp messages.

The WhatsApp connection is powered by [WhatsMeow](https://github.com/tulir/whatsmeow), a Go library for the WhatsApp Web multidevice API.

## About this adaptation

This project started from [lharries/whatsapp-mcp](https://github.com/lharries/whatsapp-mcp) and keeps its core architecture: a Go/WhatsMeow bridge connected to a Python MCP server. This fork adapts that foundation for broader, more reliable day-to-day use.

The main improvements in this adaptation include:

- Local audio transcription with NVIDIA Parakeet TDT, including automatic reuse of a compatible model installed by Handy.
- Unified direct conversations across WhatsApp phone-number JIDs and privacy-preserving LIDs.
- Unread-message tracking from real-time events and synchronized history.
- More complete messaging, group administration, contact/profile, privacy, blocklist, and WhatsApp Business label tools.
- Length-based randomized typing indicators for more natural text sending, enabled by default and configurable per message.
- Updated WhatsMeow compatibility, loopback-only REST access, safer local-data defaults, and clearer Codex/Claude installation guidance.

The original project and contributors remain credited through the Git history and license. The features above describe how this fork has been adapted; they are not intended as a line-by-line attribution of every underlying component.

> [!IMPORTANT]
> This project is not affiliated with, authorized by, or endorsed by WhatsApp or Meta. It uses an unofficial WhatsApp client. Use it only with accounts and conversations you are authorized to access, and review the [WhatsApp Terms of Service](https://www.whatsapp.com/legal/terms-of-service). Do not use it for spam, bulk messaging, or impermissible automation. Your account may be limited or banned.

![WhatsApp MCP example](./example-use.png)

## What it can do

- Search contacts, chats, and message history.
- Read text and media metadata with surrounding context.
- Download and transcribe received voice notes locally with Parakeet.
- Optionally cache received media as it arrives, before WhatsApp CDN links expire.
- Send text, files, voice notes, reactions, polls, and typing indicators.
- Edit or delete messages and send read receipts.
- Manage groups, invite links, participants, profile information, privacy settings, blocks, and business labels.
- Resolve WhatsApp phone-number JIDs and privacy-preserving LIDs as the same direct conversation.
- Add a short, length-based randomized typing delay before text messages by default. Pass `show_typing: false` to send immediately.

## Architecture

The project has two local components:

1. **`whatsapp-bridge/`** — a Go process using WhatsMeow. It links to WhatsApp, receives events, stores local state in SQLite, and exposes a REST API on `127.0.0.1:8080` (port, data directory and instance name are configurable; see [Bridge configuration](#bridge-configuration)).
2. **`whatsapp-mcp-server/`** — a Python MCP server. Your AI client launches it over stdio; it reads the local message database and calls the bridge for WhatsApp operations.

```text
AI client <-> Python MCP server <-> Go/WhatsMeow bridge <-> WhatsApp
                         |                  |
                         +---- SQLite ------+
```

Both components are required. The Go bridge stays running; the MCP client normally starts and stops the Python server automatically.

## Requirements

- A WhatsApp account and the WhatsApp mobile app for QR pairing.
- [Go 1.25+](https://go.dev/doc/install).
- [Python 3.11+](https://www.python.org/downloads/).
- [uv](https://docs.astral.sh/uv/getting-started/installation/).
- A C compiler because `go-sqlite3` uses CGO:
  - macOS: Xcode Command Line Tools (`xcode-select --install`).
  - Debian/Ubuntu: `sudo apt install build-essential`.
  - Windows: use [MSYS2](https://www.msys2.org/) and enable CGO.
- Optional: [FFmpeg](https://ffmpeg.org/download.html) for converting audio files into WhatsApp-compatible Opus voice notes.
- Optional: [parakeet-cli](https://github.com/lucataco/parakeet-cli) plus FFmpeg to transcribe received audio locally with NVIDIA Parakeet TDT 0.6B v3.

## Installation

### 1. Clone the repository

```bash
git clone https://github.com/thiavila/whatsapp-mcp.git
cd whatsapp-mcp
```

### 2. Start the WhatsApp bridge

Run the complete Go package, not only `main.go`:

```bash
cd whatsapp-bridge
go run .
```

On the first run, scan the QR code from **WhatsApp > Settings > Linked devices > Link a device**. Keep this process running.

The bridge creates `whatsapp-bridge/store/` containing the paired-device session, messages, and downloaded media. This directory is ignored by Git. Treat it as sensitive and never publish or share it.

To cache incoming images, videos, audio, and documents immediately, start the
bridge with:

```bash
WHATSAPP_EAGER_MEDIA_DOWNLOAD=true go run .
```

Eager caching is disabled by default because it increases local disk usage and
retains message attachments that would otherwise be downloaded only on demand.
The bridge limits eager downloads to two concurrent files, skips files larger
than 64 MiB, and stops eager caching when chat-media files reach 2 GiB in total.
On-demand downloads are streamed to a temporary file and limited to 512 MiB by
default. Override the limits in bytes when needed:

```bash
WHATSAPP_EAGER_MEDIA_DOWNLOAD=true \
WHATSAPP_EAGER_MEDIA_MAX_BYTES=134217728 \
WHATSAPP_EAGER_MEDIA_CACHE_MAX_BYTES=4294967296 \
WHATSAPP_MEDIA_DOWNLOAD_MAX_BYTES=1073741824 \
go run .
```

### Bridge configuration

Every setting has a default equal to the historical hard-coded value, so a
bridge started with no flags or env vars behaves exactly as before. Flags win
over environment variables.

| Setting | Flag | Environment | Default |
|---|---|---|---|
| REST port (always bound to `127.0.0.1`) | `-port` | `WHATSAPP_BRIDGE_PORT` | `8080` |
| Data directory (`whatsapp.db`, `messages.db`, media cache) | `-store-dir` | `WHATSAPP_STORE_DIR` | `store` (relative to the working directory) |
| Instance name (logs, `/api/health`, linked-device name on new pairings) | `-instance` | `WHATSAPP_BRIDGE_INSTANCE` | `default` |
| REST token | — (never a flag, so it stays out of `ps`) | `WHATSAPP_BRIDGE_TOKEN` or `WHATSAPP_BRIDGE_TOKEN_FILE` | unset |
| Auth mode | — | `WHATSAPP_BRIDGE_AUTH_MODE` = `warn` or `enforce` | `enforce` when a token is set |
| Refuse to start without a token | `-require-token` | `WHATSAPP_BRIDGE_REQUIRE_TOKEN=true` | off |
| Full history sync on a **new** pairing (days; WhatsApp caps what the phone sends) | `-full-history-days` | `WHATSAPP_FULL_HISTORY_DAYS=365` | 0 (recent history only) |

Invalid settings (bad port, token shorter than 16 characters, both token
variables set, `warn`/`enforce` without a token) stop the bridge at startup
with exit code 2. If the port is already taken the bridge exits instead of
running without its REST API.

The Python MCP server reads the same variables, so one env file can configure
a bridge instance and its MCP server:

| MCP setting | Environment | Default |
|---|---|---|
| Bridge URL | `WHATSAPP_API_BASE_URL` | `http://localhost:<WHATSAPP_BRIDGE_PORT or 8080>/api` |
| Message database | `WHATSAPP_MESSAGES_DB_PATH` | `<WHATSAPP_STORE_DIR>/messages.db` |
| WhatsMeow database | `WHATSAPP_DB_PATH` | `<WHATSAPP_STORE_DIR>/whatsapp.db` |
| REST token | `WHATSAPP_BRIDGE_TOKEN` or `WHATSAPP_BRIDGE_TOKEN_FILE` | unset (no header sent) |

A relative `WHATSAPP_STORE_DIR` is resolved by the MCP server against
`whatsapp-bridge/`; prefer absolute paths when the bridge runs elsewhere.

### REST API authentication

Without a token the REST API accepts any caller on the machine: every local
process running as any user can send messages through the paired number. The
bridge prints a large warning at startup in that case. Configure a token:

```bash
# generate once, store with chmod 600, never print it
python3 -c 'import secrets;print(secrets.token_urlsafe(32))' > ~/.config/whatsapp-bridge.token
chmod 600 ~/.config/whatsapp-bridge.token
WHATSAPP_BRIDGE_TOKEN_FILE=~/.config/whatsapp-bridge.token go run .
```

Clients must then send `Authorization: Bearer <token>` on every call; the
Python MCP server does this automatically when `WHATSAPP_BRIDGE_TOKEN` or
`WHATSAPP_BRIDGE_TOKEN_FILE` is set in its environment. Unauthenticated calls
receive HTTP 401. `GET /api/health` is the only route that does not need the
token; it returns only the instance name, connection state and auth mode.

Independently of the token, the API refuses requests whose `Host` is not a
loopback name and any request carrying an `Origin` header, which blocks
cross-site and DNS-rebinding requests from a web browser.

**Introducing a token on a running bridge without breaking clients:**

1. Update the clients first and give them the token (MCP server env, scripts
   calling the REST API). A bridge without a token ignores the header, so
   nothing changes yet.
2. Restart the bridge with the token and `WHATSAPP_BRIDGE_AUTH_MODE=warn`.
   Calls without a valid token are still served but logged as
   `[AUTH][<instance>] unauthenticated ...`.
3. Watch the log until no such line appears for a full cycle of your jobs,
   then remove `WHATSAPP_BRIDGE_AUTH_MODE` (or set it to `enforce`) and
   restart. From now on unauthenticated calls get HTTP 401.

### Running several bridges (one per number)

Each WhatsApp number needs its own bridge process with its own port, data
directory and token. For example, with systemd user units:

```ini
# ~/.config/systemd/user/whatsapp-bridge@.service
[Service]
EnvironmentFile=%h/.config/whatsapp-bridges/%i.env
WorkingDirectory=%h/srv/wa-bridges/%i
ExecStart=/path/to/whatsapp-mcp/whatsapp-bridge/whatsapp-bridge
Restart=always
```

```bash
# ~/.config/whatsapp-bridges/sales.env (chmod 600)
WHATSAPP_BRIDGE_INSTANCE=sales
WHATSAPP_BRIDGE_PORT=8081
WHATSAPP_STORE_DIR=/home/me/srv/wa-bridges/sales/store
WHATSAPP_BRIDGE_REQUIRE_TOKEN=true
WHATSAPP_BRIDGE_TOKEN_FILE=/home/me/.config/whatsapp-bridges/sales.token
```

Point that number's MCP server at the same env file (`WHATSAPP_BRIDGE_PORT`,
`WHATSAPP_STORE_DIR` and the token are shared). Check which instance owns a
port with `curl -s http://127.0.0.1:8081/api/health`.

### 3. Add the MCP server to your client

First obtain the absolute paths you will need:

```bash
which uv
cd ../whatsapp-mcp-server
pwd
```

#### Codex

```bash
codex mcp add whatsapp -- /absolute/path/to/uv \
  --directory /absolute/path/to/whatsapp-mcp/whatsapp-mcp-server \
  run main.py
```

Restart Codex after adding the server or after updating MCP tool code.

#### Claude Desktop

Add this entry to the Claude Desktop MCP configuration, replacing both absolute paths:

```json
{
  "mcpServers": {
    "whatsapp": {
      "command": "/absolute/path/to/uv",
      "args": [
        "--directory",
        "/absolute/path/to/whatsapp-mcp/whatsapp-mcp-server",
        "run",
        "main.py"
      ]
    }
  }
}
```

Common configuration locations:

- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
- Windows: `%APPDATA%/Claude/claude_desktop_config.json`

Restart Claude Desktop after changing the file.

#### Cursor and other MCP clients

Use the same command and arguments in your client's stdio MCP configuration:

```json
{
  "mcpServers": {
    "whatsapp": {
      "command": "/absolute/path/to/uv",
      "args": [
        "--directory",
        "/absolute/path/to/whatsapp-mcp/whatsapp-mcp-server",
        "run",
        "main.py"
      ]
    }
  }
}
```

## Windows notes

The bridge depends on `go-sqlite3`, which requires CGO and a C compiler:

```powershell
cd whatsapp-bridge
go env -w CGO_ENABLED=1
go run .
```

If compilation reports that `go-sqlite3 requires cgo`, verify that the MSYS2 `ucrt64\bin` directory is on `PATH` and restart the terminal.

## Local data and privacy

- WhatsMeow's paired-device credentials are stored in `whatsapp-bridge/store/whatsapp.db`.
- Message history and unread metadata are stored in `whatsapp-bridge/store/messages.db`.
- Downloaded media is stored under per-chat directories inside `whatsapp-bridge/store/`.
- The bridge REST API listens only on `127.0.0.1` (port `8080` by default); it is not intentionally exposed to the LAN. Configure a token so other local processes cannot use it.
- Message metadata remains local. Attachments are downloaded on demand by default, or saved on arrival when eager media caching is enabled. Requested messages, contact data, or media may then be included in the AI provider's context according to that client's configuration and privacy policy.

Back up the store directory if local history matters to you. Deleting it removes the local session and message database and requires pairing again.

## Security warning

This MCP server has powerful read and write capabilities. A connected agent may be able to:

- Read private messages and contact information.
- Send messages or local files.
- Download WhatsApp media to disk.
- Delete or edit messages.
- Change groups, block contacts, or modify privacy settings.

Only connect it to AI clients and projects you trust. Review tool calls before approval. Content received through WhatsApp or loaded from other untrusted sources may contain prompt-injection instructions; treat that content as data, not trusted commands.

The `send_file` and voice-note tools accept local file paths. A malicious or compromised agent could attempt to send files accessible to the MCP process. Run the MCP client with the least filesystem access practical.

## Message metadata and edit history

`messages.db` stores, next to the legacy columns, metadata taken from the
WhatsApp protocol (not from message text), so automations can identify
senders reliably in groups:

| Column | Meaning |
|---|---|
| `sender_jid` | Full sender JID without device suffix (`...@lid` or `...@s.whatsapp.net`). The legacy `sender` column keeps only the user part. |
| `sender_alt_jid` | The alternative address WhatsApp provides (phone number for a LID sender and vice versa), when present. |
| `push_name` | Display name chosen by the sender. **Not verified.** |
| `mentioned_jids` | JSON array of mentioned JIDs, or `NULL`. |
| `quoted_message_id` | ID of the message being replied to, or `NULL`. |
| `is_forwarded` | Whether WhatsApp marked the message as forwarded. |
| `edited_at` / `revoked_at` | Time of the latest edit / deletion seen for this message. |

Edits and deletions received from WhatsApp never overwrite the stored content:
each one is appended to the `message_events` table (`event_type` `edit` or
`revoke`, `target_message_id`, `sender_jid`, `new_content`, `timestamp`) and
flags the original row. Edits and deletions made through this bridge's own
`edit_message` / `delete_message` tools are logged there too (and, as before,
also update the local copy). Older databases are upgraded in place the first
time the new bridge starts; rows stored earlier keep `NULL` metadata.

`POST /api/send` returns the WhatsApp `message_id` and server `timestamp` of
the sent message alongside `success` and `message`; the `send_message` MCP
tool passes them through.

## Message sending and typing delay

`send_message` shows a typing indicator by default before sending. The delay scales with message length, includes random variation, and is bounded between 1 and 12 seconds. To skip it:

```json
{
  "recipient": "5511999999999",
  "message": "Hello!",
  "show_typing": false
}
```

This is a presentation feature, not a guarantee against WhatsApp abuse or automation controls.

## Local audio transcription with Parakeet

The `transcribe_audio` MCP tool downloads a WhatsApp audio message and
transcribes it entirely on-device. It does not call a cloud transcription API.

On Apple Silicon macOS, install the CLI with Homebrew:

```bash
brew install lucataco/tap/parakeet-cli
```

If you already use [Handy](https://github.com/cjpais/Handy) with
`parakeet-tdt-0.6b-v3`, the MCP detects and reuses its ONNX INT8 model. Otherwise
download the model once:

```bash
parakeet download
```

You can also point to a compatible model explicitly:

```bash
export PARAKEET_MODEL_DIR="/absolute/path/to/parakeet-tdt-0.6b-v3-int8"
```

WhatsApp voice notes are normally OGG/Opus, so FFmpeg must be available on
`PATH`. The temporary WAV used during transcription is deleted immediately.

## Troubleshooting

### The QR code does not appear

- Confirm that the bridge has no existing paired session in `whatsapp-bridge/store/whatsapp.db`.
- Run it in an interactive terminal with `go run .`.
- If your terminal cannot render the QR art, look for the value between `[QR_RAW]` and `[/QR_RAW]` in the output.

### The MCP reports `Transport closed`

Restart the MCP host application. Existing stdio MCP processes do not reload Python tool definitions after the source changes.

### The bridge cannot bind port 8080

Another process is already using the bridge port, and the bridge exits with `Failed to start REST API server`. Stop the other bridge instance, or give this one another port with `-port` / `WHATSAPP_BRIDGE_PORT` (and point its MCP server at the same port).

### The MCP reports `HTTP 401`

The bridge has a token and the MCP server is not sending it (or sends another one). Set `WHATSAPP_BRIDGE_TOKEN` or `WHATSAPP_BRIDGE_TOKEN_FILE` in the MCP server's environment to the bridge's value and restart the MCP host.

### New messages are missing

- Confirm that the bridge is still connected and running.
- Initial history synchronization can take several minutes.
- Restart the bridge before considering a new pairing.
- Deleting `whatsapp-bridge/store/` is a last resort because it removes the local session and history.

### Phone-number and LID conversations look separate

Recent WhatsApp versions may deliver replies under a privacy-preserving LID while sends use a phone-number JID. This fork resolves WhatsMeow's PN/LID mapping when querying direct chats.

## Development

Run the Python tests:

```bash
cd whatsapp-mcp-server
uv run python -m unittest discover -s tests -v
```

Build or test the Go bridge:

```bash
cd whatsapp-bridge
go test ./...
go build ./...
```

Generated databases, paired-device credentials, logs, media, virtual environments, and compiled binaries must not be committed.

## Project lineage

- Original MCP project: [lharries/whatsapp-mcp](https://github.com/lharries/whatsapp-mcp)
- WhatsApp protocol library: [tulir/whatsmeow](https://github.com/tulir/whatsmeow)
- Unread tracking work originated in [lharries/whatsapp-mcp#59](https://github.com/lharries/whatsapp-mcp/pull/59) by [@maxprokopp](https://github.com/maxprokopp).

Notable changes in this fork include a current WhatsMeow dependency, loopback-only bridge API, expanded messaging/group/contact/business tools, unread tracking, PN/LID identity resolution, and optional natural typing delay.

## License

[MIT](./LICENSE). WhatsMeow is a separate dependency distributed under its own [MPL-2.0 license](https://github.com/tulir/whatsmeow/blob/main/LICENSE).
