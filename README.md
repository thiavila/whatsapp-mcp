# WhatsApp MCP

An unofficial, local-first [Model Context Protocol (MCP)](https://modelcontextprotocol.io/) server that lets compatible AI clients search, read, and send WhatsApp messages.

The WhatsApp connection is powered by [WhatsMeow](https://github.com/tulir/whatsmeow), a Go library for the WhatsApp Web multidevice API. This project is a maintained fork of [lharries/whatsapp-mcp](https://github.com/lharries/whatsapp-mcp).

> [!IMPORTANT]
> This project is not affiliated with, authorized by, or endorsed by WhatsApp or Meta. It uses an unofficial WhatsApp client. Use it only with accounts and conversations you are authorized to access, and review the [WhatsApp Terms of Service](https://www.whatsapp.com/legal/terms-of-service). Do not use it for spam, bulk messaging, or impermissible automation. Your account may be limited or banned.

![WhatsApp MCP example](./example-use.png)

## What it can do

- Search contacts, chats, and message history.
- Read text and media metadata with surrounding context.
- Send text, files, voice notes, reactions, polls, and typing indicators.
- Edit or delete messages and send read receipts.
- Manage groups, invite links, participants, profile information, privacy settings, blocks, and business labels.
- Resolve WhatsApp phone-number JIDs and privacy-preserving LIDs as the same direct conversation.
- Add a short, length-based randomized typing delay before text messages by default. Pass `show_typing: false` to send immediately.

## Architecture

The project has two local components:

1. **`whatsapp-bridge/`** — a Go process using WhatsMeow. It links to WhatsApp, receives events, stores local state in SQLite, and exposes a REST API on `127.0.0.1:8080`.
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
- The bridge REST API listens only on `127.0.0.1:8080`; it is not intentionally exposed to the LAN.
- Data remains local until an MCP client requests it. Requested messages, contact data, or media may then be included in the AI provider's context according to that client's configuration and privacy policy.

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

Another process is already using the bridge port. Stop the other bridge instance and run `go run .` again.

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
