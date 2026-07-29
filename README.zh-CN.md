# WeCom MCP Server

![Social Preview](docs/images/social-preview.jpg)

基于 Python 实现的企业微信（WeCom）MCP (Model Context Protocol) Server。

提供一组标准的 MCP 工具，使 AI Agent（如 Trae Work / Claude Desktop）能够通过调用工具的方式，实现向企业微信用户/部门发送消息、查询通讯录、接收与存储消息等功能。

> 🌐 [English Version](README.md)

## ✨ 功能特性

### 核心能力
- **📨 消息发送**：支持文本、Markdown、通用消息体、文件发送及 `@all` 群发
- **📇 通讯录辅助**：根据成员姓名快速查找 UserID，按部门批量获取成员信息
- **📥 消息接收**：HTTP 模式下自动监听企业微信回调，将接收到的消息实时持久化到 SQLite
- **🔍 历史查询**：提供按时间、类型、发送人等条件查询历史消息的工具
- **🗄️ 文件管理**：接收到的图片/语音/视频/文件会自动下载到本地磁盘，数据库中记录元信息

### 技术亮点
- **纯 Python AES-256-CBC**：不依赖 `cryptography` 库，避免了在某些平台上的原生编译依赖问题
- **多 Transport 支持**：`stdio`（单 Agent）、`streamable-http`（多 Agent 共用）、`sse`（旧版兼容）
- **SQLite 按年分表**：自动按自然年拆分数据表，防止单表数据过大
- **基于 `uv` 管理**：极速的包管理器与虚拟环境

## 🛠️ MCP 工具列表

### 通用工具 (Stdio & HTTP/SSE 模式均可用)

| 工具名称 | 描述 |
| :--- | :--- |
| `send_text_message` | 发送文本消息 |
| `send_markdown_message` | 发送 Markdown 消息 |
| `send_message` | 发送任意类型消息 (text/markdown/news 等) |
| `send_file_message` | 发送本地文件 (自动上传) |
| `mass_send_message` | 向应用可见范围**@所有人**群发消息 |
| `lookup_user_id` | 通过成员姓名查找 UserID |
| `list_users_by_department` | 列出指定部门（含子部门）下的所有成员 |
| `check_config` | 检查当前 WeCom 配置状态 |

### HTTP/SSE 专属工具

| 工具名称 | 描述 |
| :--- | :--- |
| `health_check` | 健康检查 (Webhook 状态、数据库状态等) |
| `search_history_messages` | 查询历史消息 (支持时间/类型/发送人/关键词过滤) |
| `get_message_detail` | 获取单条消息详情 |
| `get_message_file` | 获取消息关联的本地文件路径 |
| `list_message_years` | 列出有历史数据的年份 |
| `get_message_stats` | 历史消息统计 (按类型/人/月份) |

## 🚀 快速开始

### 1. 环境要求

- Python >= 3.14
- [uv](https://github.com/astral-sh/uv) 包管理器

### 2. 安装与配置

```bash
# 克隆项目
git clone <your-repo-url>
cd wecom-mcp-server

# 创建虚拟环境并安装依赖
uv sync

# 复制环境变量配置模板
cp .env.example .env
```

### 3. 配置

编辑 `.env` 文件，填写企业微信自建应用的凭证：

```env
# 企业 ID (CorpID)
WECOM_CORP_ID=ww1234567890abcdef

# 自建应用 Secret
WECOM_CORP_SECRET=your_app_secret

# 自建应用 AgentId (数字)
WECOM_AGENT_ID=1000002

# ====== 可选配置 ======

# 通讯录同步 Secret (用于全量读取通讯录，推荐配置)
WECOM_CONTACTS_SECRET=your_contacts_secret

# 接收消息回调 (仅在需要接收消息/历史查询时配置)
WECOM_CALLBACK_TOKEN=your_callback_token
WECOM_CALLBACK_ENCODING_AES_KEY=your_aes_key_43_chars_long

# MCP 服务模式
# stdio: 默认，单客户端本地调用
# streamable-http: HTTP 服务，可被多个 Agent 调用
# sse: 旧版 SSE 模式
WECOM_MCP_TRANSPORT=streamable-http
WECOM_MCP_HOST=0.0.0.0
WECOM_MCP_PORT=9000

# 数据持久化目录
WECOM_DATA_DIR=data
```

### 4. 运行

#### 模式一：本地 Stdio 模式 (供 Trae Work 调用)

在 Trae Work 的 MCP 配置文件中添加：

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

#### 模式二：HTTP 服务模式 (供多 Agent 共用)

```bash
# 前台运行
uv run wecom-mcp-server --transport streamable-http --host 0.0.0.0 --port 9000

# 或使用 .env 配置
WECOM_MCP_TRANSPORT=streamable-http WECOM_MCP_PORT=9000 uv run wecom-mcp-server
```

调用方通过 HTTP 请求与 MCP Server 交互：

```bash
curl -X POST http://localhost:9000/mcp \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "send_text_message", "arguments": {"to_user": "zhangsan", "content": "Hello!"}}}'
```

## ⚙️ Supervisor 守护进程部署

如果希望服务在后台常驻运行，可以使用 `supervisord` 进行管理。

配置文件路径：`/usr/local/etc/supervisor.d/wecom-mcp-server.conf`

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

常用命令：

```bash
supervisorctl reread            # 重新加载配置
supervisorctl update            # 更新进程组
supervisorctl status            # 查看所有服务状态
supervisorctl restart wecom-mcp-server  # 重启服务
```

## 📁 项目结构

```
wecom-mcp-server/
├── src/
│   └── wecom_mcp_server/
│       ├── __init__.py
│       ├── server.py          # MCP Server 入口，工具注册
│       ├── config.py          # 配置管理 (pydantic-settings)
│       ├── aes.py             # 纯 Python AES-256-CBC 实现
│       ├── crypto.py          # 企业微信消息加解密逻辑
│       ├── wecom_client.py    # 企业微信 API 异步客户端
│       ├── webhook.py         # 接收消息的 Webhook 服务
│       └── storage.py         # SQLite 消息持久化与文件管理
├── .env.example               # 环境变量示例
├── pyproject.toml             # 项目依赖与构建配置
└── README.md                  # 英文文档 (默认)
└── README.zh-CN.md            # 本文档
```

## 🔑 配置说明

### 企业微信应用权限

1. **基础发送权限**：需在企业微信后台创建自建应用，并获取 `CorpID`, `Secret`, `AgentId`。
2. **通讯录读取权限**（可选）：在"管理工具 → 通讯录同步"中开启 `API接口同步`，获取 `contacts_secret`。如不配置，仅能读取应用可见范围内的成员。
3. **接收消息**（可选）：在应用的"接收消息"设置中开启 API 接收，获取 `Token` 和 `EncodingAESKey`。本地开发时需使用内网穿透工具（如 ngrok, cloudflared）暴露回调地址。

### 数据存储结构

```
data/
├── wecom_messages.db          # SQLite 数据库 (按年分表)
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
