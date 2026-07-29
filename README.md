# WeCom MCP Server

![Social Preview](docs/images/social-preview.jpg)

A Python-based MCP (Model Context Protocol) Server for WeCom (WeChat Work / 企业微信) self-built applications.

Provides a standard set of MCP tools that enable AI Agents (e.g., Trae Work, Claude Desktop) to send messages to WeCom users/departments, query contacts, receive and store messages, and more.

> 🌐 [中文版 (Chinese)](README.zh-CN.md)

## ✨ Features

### Core Capabilities
- **📨 Message Sending**: Text, Markdown, generic message bodies, file sending, and `@all` mass messaging
- **📇 Contact Lookup**: Quickly find UserIDs by member name, or batch-retrieve members by department
- **📥 Message Reception**: In HTTP mode, automatically listens for WeCom callbacks and persists received messages to SQLite in real time
- **🔍 History Query**: Query historical messages by time, type, sender, keywords, and more
- **🗄️ File Management**: Received images/voice/video/files are automatically downloaded to local disk with metadata stored in the database

### Technical Highlights
- **Pure-Python AES-256-CBC**: No dependency on the `cryptography` library, avoiding native compilation issues on certain platforms
- **Multiple Transport Support**: `stdio` (single Agent), `streamable-http` (shared by multiple Agents), `sse` (legacy compatibility)
- **SQLite Yearly Partitioning**: Automatically splits data tables by calendar year to prevent single-table bloat
- **Managed by `uv`**: Ultra-fast package manager and virtual environment

## 🛠️ MCP Tool List

### Universal Tools (Stdio & HTTP/SSE)

| Tool Name | Description |
| :--- | :--- |
| `send_text_message` | Send a text message |
| `send_markdown_message` | Send a Markdown message |
| `send_message` | Send any type of message (text/markdown/news, etc.) |
| `send_file_message` | Send a local file (auto-upload) |
| `mass_send_message` | Mass message all members in the app's visible scope (`@all`) |
| `lookup_user_id` | Find UserID by member name |
| `list_users_by_department` | List all members in a department (including sub-departments) |
| `check_config` | Check current WeCom configuration status |

### HTTP/SSE Exclusive Tools

| Tool Name | Description |
| :--- | :--- |
| `health_check` | Health check (Webhook status, database status, etc.) |
| `search_history_messages` | Query historical messages (filter by time/type/sender/keywords) |
| `get_message_detail` | Get details of a single message |
| `get_message_file` | Get the local file path associated with a message |
| `list_message_years` | List years that have historical data |
| `get_message_stats` | Historical message statistics (by type/sender/month) |

## 🚀 Quick Start

### 1. Requirements

- Python >= 3.14
- [uv](https://github.com/astral-sh/uv) package manager

### 2. Installation & Configuration

```bash
# Clone the project
git clone <your-repo-url>
cd wecom-mcp-server

# Create virtual environment and install dependencies
uv sync

# Copy the environment variable template
cp .env.example .env
```

### 3. Configuration

Edit `.env` and fill in your WeCom self-built application credentials:

```env
# Corp ID
WECOM_CORP_ID=ww1234567890abcdef

# Self-built App Secret
WECOM_CORP_SECRET=your_app_secret

# Self-built App AgentId (number)
WECOM_AGENT_ID=1000002

# ====== Optional ======

# Contacts Sync Secret (recommended for full-directory access)
WECOM_CONTACTS_SECRET=your_contacts_secret

# Message callback (only needed for receiving messages / history query)
WECOM_CALLBACK_TOKEN=your_callback_token
WECOM_CALLBACK_ENCODING_AES_KEY=your_aes_key_43_chars_long

# MCP service mode
# stdio: default, single-client local calls
# streamable-http: HTTP service, shared by multiple Agents
# sse: legacy SSE mode
WECOM_MCP_TRANSPORT=streamable-http
WECOM_MCP_HOST=0.0.0.0
WECOM_MCP_PORT=9000

# Data persistence directory
WECOM_DATA_DIR=data
```

### 4. Run

#### Mode 1: Stdio (for Trae Work)

Add to your Trae Work MCP configuration:

```json
{
  "mcpServers": {
    "wecom": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/wecom-mcp-server", "wecom-mcp-server"]
    }
  }
}
```

#### Mode 2: HTTP Service (shared by multiple Agents)

```bash
# Run in foreground
uv run wecom-mcp-server --transport streamable-http --host 0.0.0.0 --port 9000

# Or use .env configuration
WECOM_MCP_TRANSPORT=streamable-http WECOM_MCP_PORT=9000 uv run wecom-mcp-server
```

Callers interact with the MCP Server via HTTP:

```bash
curl -X POST http://localhost:9000/mcp \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "send_text_message", "arguments": {"to_user": "zhangsan", "content": "Hello!"}}}'
```

## ⚙️ Supervisor Deployment

To keep the service running in the background, use `supervisord`.

Config file: `/usr/local/etc/supervisor.d/wecom-mcp-server.conf`

```ini
[program:wecom-mcp-server]
command=/usr/local/bin/uv run wecom-mcp-server --transport streamable-http --host 0.0.0.0 --port 9000
directory=/path/to/wecom-mcp-server
user=your_username
autostart=true
autorestart=true
startretries=3
stopasgroup=true
killasgroup=true
stderr_logfile=/path/to/logs/wecom-mcp-server.err.log
stdout_logfile=/path/to/logs/wecom-mcp-server.out.log
environment=LANG="en_US.UTF-8",PATH="/usr/local/bin:/usr/bin:/bin"
```

Common commands:

```bash
supervisorctl reread            # Reload configuration
supervisorctl update            # Update process groups
supervisorctl status            # View all service statuses
supervisorctl restart wecom-mcp-server  # Restart service
```

## 📁 Project Structure

```
wecom-mcp-server/
├── src/
│   └── wecom_mcp_server/
│       ├── __init__.py
│       ├── server.py          # MCP Server entry point, tool registration
│       ├── config.py          # Configuration management (pydantic-settings)
│       ├── aes.py             # Pure-Python AES-256-CBC implementation
│       ├── crypto.py          # WeCom message encryption/decryption logic
│       ├── wecom_client.py    # WeCom API async client
│       ├── webhook.py         # Message reception webhook service
│       └── storage.py         # SQLite message persistence & file management
├── .env.example               # Environment variable example
├── pyproject.toml             # Project dependencies & build config
└── README.md                  # This document
```

## 🔑 Configuration Notes

### WeCom App Permissions

1. **Basic Sending Permission**: Create a self-built app in the WeCom admin backend and obtain `CorpID`, `Secret`, and `AgentId`.
2. **Contact Reading Permission** (optional): Enable `API Interface Sync` in "Management Tools → Contact Sync" to get `contacts_secret`. Without it, only members within the app's visible scope can be read.
3. **Message Reception** (optional): Enable API reception in the app's "Receive Messages" settings to get `Token` and `EncodingAESKey`. For local development, use an intranet tunnel tool (e.g., ngrok, cloudflared) to expose the callback address.

### Data Storage Structure

```
data/
├── wecom_messages.db          # SQLite database (yearly partitioned tables)
└── files/
    ├── 2026/
    │   ├── 07/
    │   │   ├── image_xxx.jpg
    │   │   └── report.pdf
    │   └── ...
    └── ...
```

## 📜 License

[MIT](LICENSE)
