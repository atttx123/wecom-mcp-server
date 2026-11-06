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
- **✅ Approval Integration**: Connects to the "Workbench - Approval" open APIs to export approval submission records by time / template / applicant / status, with built-in statistics

### Technical Highlights
- **Pure-Python AES-256-CBC**: No dependency on the `cryptography` library, avoiding native compilation issues on certain platforms
- **Multiple Transport Support**: `stdio` (single Agent), `streamable-http` (shared by multiple Agents), `sse` (legacy compatibility)
- **SQLite Yearly Partitioning**: Automatically splits data tables by calendar year to prevent single-table bloat
- **Dependency-Free XLSX Writer**: Built-in OOXML writer produces real Excel workbooks (multi-sheet, frozen header, auto-filter) even in offline environments
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

### Approval Tools (Universal — Stdio & HTTP/SSE)

Backed by the "Workbench - Approval" open APIs. The accessible data range is determined by both the "Approval Data Permission" grant and the self-built app's visible scope.

| Tool Name | Description |
| :--- | :--- |
| `export_approval_records` | Export approval submission records by filters (CSV / XLSX / JSON) with statistics |
| `analyze_approval_records` | Analyze exported records (template / status / applicant / duration / amount) |
| `get_approval_detail` | Get one approval record (form content, approval flow, comments) |
| `list_approval_templates` | List approval templates and their control schema |
| `search_approval_records` | Search form content across exported records |

> `department_id` filters by **department subtree**: the WeCom server-side `department` filter already includes child departments, and the local second-pass filter matches the same way, so passing a parent department never drops its children's records.

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
WECOM_MCP_PORT=8101

# Data persistence directory
WECOM_DATA_DIR=data

# Concurrency for approval detail fetching (WeCom API limit: 600 requests/minute)
WECOM_APPROVAL_CONCURRENCY=8
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
uv run wecom-mcp-server --transport streamable-http --host 0.0.0.0 --port 8101

# Or use .env configuration
WECOM_MCP_TRANSPORT=streamable-http WECOM_MCP_PORT=8101 uv run wecom-mcp-server
```

Callers interact with the MCP Server via HTTP:

```bash
curl -X POST http://localhost:8101/mcp \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "send_text_message", "arguments": {"to_user": "zhangsan", "content": "Hello!"}}}'
```

## ✅ Approval Feature (Workbench - Approval)

### Prerequisites (WeCom Admin Console)

Three grants are required:

1. **App Management → Approval → API → Callable Apps**: add the self-built app used by this service;
2. **App Management → Approval → API → Approval Data Permission**: authorize the app to read approval data;
3. **Self-built app visible scope**: must cover the target applicants — records outside the scope are filtered server-side.

Also add the machine's egress IP under **App Management → Self-built App → Enterprise Trusted IP**,
otherwise the API returns `errcode=60020`.

Without these grants the API returns `errcode=301055` / `60020`; the tool surfaces the matching guidance directly.

### Name & Department Resolution

The approval detail API returns only `userid` — **never names**. Names are resolved through the
contacts API, with the following fallback chain:

| Priority | Source | Coverage | Requirement |
| :--- | :--- | :--- | :--- |
| 1 | `WECOM_CONTACTS_SECRET` (contact sync) | Whole company | Management Tools → Contact Sync → **enable "API Interface Sync"** |
| 2 | The app's own secret (automatic fallback) | Members **within the app's visible scope** | No extra setup |

If `WECOM_CONTACTS_SECRET` is set but "API Interface Sync" was never enabled, the API returns
`48009` and the service transparently falls back to the app secret — names and departments
still resolve as long as the applicants are inside the app's visible scope.

> Note: `department_id=0` (whole company) is supported **only by the contact-sync secret**;
> with the app secret the server replies `60003`. The service degrades automatically to
> "fetch the department list, then pull members per top-level department" — no manual step needed.

### Typical Calls

```jsonc
// 1) Export last month's reimbursements, filtered by template / applicant / status
{
  "name": "export_approval_records",
  "arguments": {
    "start_date": "2026-09-01",
    "end_date": "2026-09-30",
    "template_name": "报销",
    "sp_status": "审批中,已通过",
    "output_format": "xlsx",
    "output_path": "data/exports/2026-09-reimbursements.xlsx"
  }
}

// 2) Group statistics over exported data (pass rate, duration, amounts)
{ "name": "analyze_approval_records", "arguments": { "start_date": "2026-09-01" } }

// 3) Search approvals whose form content contains a keyword
{ "name": "search_approval_records", "arguments": { "keyword": "travel" } }
```

### Exported Columns

| Column | Description |
| :--- | :--- |
| Approval No. / Template Name / Template ID | Record identity and owning template |
| Status | In approval / Approved / Rejected / Withdrawn / Withdrawn after approval / Deleted / Paid |
| Submitted At / Completed At / Duration (h) | Completion time = last action timestamp across approval nodes |
| Applicant / UserID / Department | Names resolved from the contacts API (subject to visible scope) |
| Amount Total | Sum of top-level money controls (nested detail amounts are reported separately to avoid double counting) |
| Form Content | Every control rendered as readable text, with detail-table rows expanded |
| Approval Flow | Node index, approval mode (single / countersign), approver, comment and timestamp |
| Comments / Node Count / Attachment Count / Notified | Supplementary approval metadata |

XLSX output contains multiple sheets: `审批明细` / `按模板统计` / `按状态统计` / `按申请人统计` / `金额汇总`.

### Analysis Dimensions

Status distribution, per-template breakdown (pass rate / average duration / amount), top applicants, submissions per day and per month, approval duration (mean / median / max / min), per-field amount aggregation, plus derived conclusions (overall pass rate, lowest pass-rate template, slowest template, overdue pending records).

### API Limits (handled automatically in the service layer)

| Limit | Handling |
| :--- | :--- |
| `getapprovalinfo` time span ≤ 31 days | Split into 31-day windows and fetch each |
| 100 records per page | Automatic cursor pagination |
| 600 requests/minute | Detail fetch concurrency tuned by `WECOM_APPROVAL_CONCURRENCY` (default 4) |
| Global per-company concurrency cap on the same API (`45033`) | Concurrency stays low; on `45033` the client backs off and retries, and any records that still fail are reported explicitly in `notes` |
| A single failing detail | Logged and skipped; the rest of the export still succeeds |

## ⚙️ Supervisor Deployment

The service is managed by the **system-wide supervisord**, with its config at
`/usr/local/etc/supervisor.d/wecom-mcp-server.conf`.

```ini
[program:wecom-mcp-server]
command=/path/to/wecom-mcp-server/scripts/run_mcp_server.sh
directory=/path/to/wecom-mcp-server
user=your_username
autostart=true
autorestart=true
startretries=3
stopasgroup=true
killasgroup=true
stderr_logfile=/path/to/logs/wecom-mcp-server.err.log
stdout_logfile=/path/to/logs/wecom-mcp-server.out.log
environment=LANG="en_US.UTF-8",PATH="/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",PYTHONUNBUFFERED="1",WECOM_API_TRUST_ENV="false",no_proxy="*",NO_PROXY="*",WECOM_MCP_HOST="127.0.0.1",WECOM_MCP_PORT="8101"
```

Common commands:

```bash
supervisorctl status wecom-mcp-server   # Check status
supervisorctl restart wecom-mcp-server  # Restart (required after editing .env)
supervisorctl stop wecom-mcp-server     # Stop
supervisorctl reread && supervisorctl update  # Reload after config changes
```

### Configuration notes

| Item | Value | Notes |
| :--- | :--- | :--- |
| Command | `scripts/run_mcp_server.sh` | Launch wrapper that clears stale port holders before starting the server (see below) |
| `autostart` | `true` | Start automatically with the system |
| Bind address | `127.0.0.1` | The MCP protocol has **no authentication** — binding `0.0.0.0` lets any host on the network call the WeCom API |
| `PATH` | must include `/usr/sbin` | On macOS `lsof` lives at `/usr/sbin/lsof`; omitting it silently disables the wrapper's port detection |
| Egress | `WECOM_API_TRUST_ENV=false` + `no_proxy=*` | **Required**, see below |
| `.env` changes | need `restart` | `.env` is read by `pydantic-settings` at process start |

### ⚠️ Why a launch wrapper is needed

When supervisord is `SIGKILL`ed (rather than shut down gracefully), the children it spawned
**do not exit with it** — they become orphans that keep holding the TCP port. The next
supervisord instance then fails to bind with `address already in use` and loops forever in
`STARTING`, never actually serving.

`scripts/run_mcp_server.sh` handles this in two steps:

1. Locate whatever holds the target port via `lsof`, cross-check it against the PID file, and
   terminate **only instances belonging to this project** (matching working directory or PID
   file) — other services on the same port are never touched;
2. Write `data/run/mcp_server.pid` so the next start can identify leftovers quickly.

The script launches the real server via `exec`, so supervisord still tracks the server
process itself and `stop`/`restart` behave exactly as before.

### ⚠️ Why daemons must disable proxy inheritance

`httpx` honors `HTTP_PROXY` / `HTTPS_PROXY` by default. A daemon inherits the environment of
the session that launched it — if that proxy comes from a short-lived component (an IDE
sandbox, for example), it dies with the session, after which the service fails permanently
with `All connection attempts failed`, even though the very same command works fine from a
terminal.

The config therefore sets `WECOM_API_TRUST_ENV=false` and clears `no_proxy`, so the service
reaches the WeCom API directly and is never coupled to a transient session. If your production
environment genuinely requires a proxy, point `environment` at a stable proxy address instead.

### Connecting an MCP client

- **streamable-http** (default here): `{"type": "http", "url": "http://127.0.0.1:8101/mcp"}`
- **stdio**: no supervisord needed — the MCP client spawns the process itself. Keep
  `WECOM_API_TRUST_ENV=true` (or simply leave the proxy variables unset).

### Single-instance constraint

The message and approval databases (`data/*.db`) are local SQLite files that **only one
process may write**. Do not run a supervisord instance alongside a manually started
process — concurrent writers can trigger database lock contention.

The launch wrapper automatically clears stale instances **belonging to this project**, but
never touches processes you started yourself — if a manual instance is holding the port, the
managed one logs a warning and fails to bind.

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
│       ├── wecom_client.py    # WeCom API async client (incl. approval open APIs)
│       ├── webhook.py         # Message reception webhook service
│       ├── storage.py         # SQLite message persistence & file management
│       ├── approval.py        # Approval domain layer: control parsing, flow parsing, collector
│       ├── approval_storage.py # Approval persistence & query (SQLite)
│       ├── approval_export.py # Approval export (CSV/XLSX/JSON) & statistics
│       └── xlsx_writer.py     # Dependency-free XLSX writer
├── tests/
│   ├── fixtures/              # Sample approval payloads
│   ├── test_approval.py       # Offline approval tests (no WeCom credentials needed)
│   ├── test_mcp_live.py       # MCP protocol integration test (streamable-http)
│   └── test_mcp_stdio.py      # MCP protocol integration test (stdio)
├── scripts/
│   └── run_mcp_server.sh      # supervisord launch wrapper (clears stale port holders)
├── .env.example               # Environment variable example
├── pyproject.toml             # Project dependencies & build config
└── README.md                  # This document
```

## 🔑 Configuration Notes

### WeCom App Permissions

1. **Basic Sending Permission**: Create a self-built app in the WeCom admin backend and obtain `CorpID`, `Secret`, and `AgentId`.
2. **Contact Reading Permission** (optional): Enable `API Interface Sync` in "Management Tools → Contact Sync" to get `contacts_secret`. Without it, only members within the app's visible scope can be read.
3. **Message Reception** (optional): Enable API reception in the app's "Receive Messages" settings to get `Token` and `EncodingAESKey`. For local development, use an intranet tunnel tool (e.g., ngrok, cloudflared) to expose the callback address.
4. **Approval Data Permission**: In "App Management → Approval → API", add the self-built app to "Callable Apps" and grant the approval data permission — see [Approval Feature](#-approval-feature-workbench---approval).

### Data Storage Structure

```
data/
├── wecom_messages.db          # SQLite database (messages, yearly partitioned)
├── wecom_approvals.db         # SQLite database (approvals + template metadata)
├── exports/                   # Approval exports (csv / xlsx / json)
└── files/
    ├── 2026/
    │   ├── 07/
    │   │   ├── image_xxx.jpg
    │   │   └── report.pdf
    │   └── ...
    └── ...
```

### Running Tests

```bash
uv run python tests/test_approval.py
```

## 📜 License

[MIT](LICENSE)
