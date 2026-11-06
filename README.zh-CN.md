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
- **✅ 审批对接**：对接「工作台 - 审批」开放接口，按时间/模板/申请人/状态等条件导出审批提交记录，并输出统计分析

### 技术亮点
- **纯 Python AES-256-CBC**：不依赖 `cryptography` 库，避免了在某些平台上的原生编译依赖问题
- **多 Transport 支持**：`stdio`（单 Agent）、`streamable-http`（多 Agent 共用）、`sse`（旧版兼容）
- **SQLite 按年分表**：自动按自然年拆分数据表，防止单表数据过大
- **零依赖 XLSX 导出**：内置 OOXML 写出器，离线环境也能直接产出 Excel 文件（多工作表 + 冻结表头 + 自动筛选）
- **基于 `uv` 管理**：极速的包管理器与虚拟环境

## 🛠️ MCP 工具列表

### 通用工具 (Stdio & HTTP/SSE 模式均可用)

| 工具名称 | 描述 |
| :--- | :--- |
| `send_text_message` | 发送文本消息 |
| `send_markdown_message` | 发送 Markdown 消息 |
| `send_message` | 发送任意类型消息；`msg_type` 为 image/voice/video/file 时给 `file_path` 会自动上传；`to_user="@all"` 即群发应用可见范围全部成员 |
| `lookup_user_id` | 通过成员姓名查找 UserID |
| `list_users_by_department` | 列出指定部门（含子部门）下的所有成员 |

### 审批工具（通用，Stdio & HTTP/SSE 模式均可用）

对接企业微信「工作台 - 审批」应用，数据范围由「审批数据权限」与自建应用可见范围共同决定。

| 工具名称 | 描述 |
| :--- | :--- |
| `export_approval_records` | 按条件导出审批提交记录（CSV / XLSX / JSON），同时返回统计分析 |
| `get_approval_detail` | 获取单条审批单详情（表单内容、审批流程、备注） |
| `list_approval_templates` | 列出审批模板及控件结构，用于确定筛选条件 |
| `search_approval_records` | 在本地已导出的审批记录中检索表单内容；加 `with_stats=True` 附带分组统计 |

> `department_id` 按**部门子树**筛选：企业微信服务端的 `department` 过滤本身即包含子部门，本地二次筛选也按子树判断，因此传父部门不会漏掉子部门的审批单。

### HTTP/SSE 专属工具

| 工具名称 | 描述 |
| :--- | :--- |
| `health_check` | 健康检查 (Webhook 状态、数据库状态等)；加 `include_config=True` 附带配置摘要 |
| `search_history_messages` | 查询历史消息 (支持时间/类型/发送人/关键词过滤) |
| `get_message_detail` | 获取单条消息详情；加 `resolve_file=True` 附带文件本地绝对路径 |
| `get_message_stats` | 历史消息统计 (按类型/人/月份)；加 `include_years=True` 附带各年份条数 |

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
WECOM_MCP_PORT=8101

# 数据持久化目录
WECOM_DATA_DIR=data

# 审批详情拉取并发数（企业微信接口频率上限 600 次/分钟）
WECOM_APPROVAL_CONCURRENCY=8
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
uv run wecom-mcp-server --transport streamable-http --host 0.0.0.0 --port 8101

# 或使用 .env 配置
WECOM_MCP_TRANSPORT=streamable-http WECOM_MCP_PORT=8101 uv run wecom-mcp-server
```

调用方通过 HTTP 请求与 MCP Server 交互：

```bash
curl -X POST http://localhost:8101/mcp \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "send_text_message", "arguments": {"to_user": "zhangsan", "content": "Hello!"}}}'
```

## ✅ 审批功能（工作台 - 审批）

### 前置配置（企业微信管理后台）

调用审批开放接口需要三步授权，缺一不可：

1. **应用管理 → 审批 → API → 可调用应用**：把本服务使用的自建应用加入列表；
2. **应用管理 → 审批 → API → 审批数据权限**：勾选授权该应用「允许提交/读取审批单据」；
3. **自建应用可见范围**：需覆盖目标申请人，否则接口只会返回可见范围内成员的审批单（越权数据会被服务端过滤）。

另外还需在 **应用管理 → 自建应用 → 企业可信 IP** 中加入本机出口 IP，否则返回 `errcode=60020`。

未配置时调用会返回 `errcode=301055` / `60020`，工具会直接给出对应指引。

### 姓名与部门解析

审批详情接口只返回 `userid`，**不含姓名**——姓名需通过通讯录接口解析。本服务按以下顺序降级：

| 优先级 | 数据来源 | 覆盖范围 | 前提 |
| :--- | :--- | :--- | :--- |
| 1 | `WECOM_CONTACTS_SECRET`（通讯录同步） | 全公司 | 管理工具 → 通讯录同步 → **开启「API 接口同步」** |
| 2 | 应用自身 secret（自动回退） | **应用可见范围内**成员 | 无额外配置 |

若填了 `WECOM_CONTACTS_SECRET` 但未开启「API 接口同步」，接口会返回 `48009`，服务会自动回退到应用 secret——只要目标申请人在应用可见范围内，姓名/部门仍可正常解析。

> 注意：企业微信的 `department_id=0`（全公司）**仅通讯录同步 secret 可用**；用应用 secret 时服务端返回 `60003`。服务会自动降级为「先取部门列表，再按顶级部门拉成员」，无需人工干预。

### 典型调用

```jsonc
// 1) 导出上月全部报销单，并按模板/申请人/状态过滤
{
  "name": "export_approval_records",
  "arguments": {
    "start_date": "2026-09-01",
    "end_date": "2026-09-30",
    "template_name": "报销",
    "sp_status": "审批中,已通过",
    "output_format": "xlsx",
    "output_path": "data/exports/2026-09报销.xlsx"
  }
}

// 2) 对已导出数据做分组分析（模板通过率、平均时长、金额汇总）
{ "name": "search_approval_records", "arguments": { "start_date": "2026-09-01", "with_stats": true } }

// 3) 检索表单内容中包含关键词的审批单
{ "name": "search_approval_records", "arguments": { "keyword": "差旅" } }
```

### 导出内容

| 列 | 说明 |
| :--- | :--- |
| 审批单号 / 模板名称 / 模板ID | 单据标识与所属模板 |
| 审批状态 | 审批中 / 已通过 / 已驳回 / 已撤销 / 通过后撤销 / 已删除 / 已支付 |
| 提交时间 / 完成时间 / 审批时长(小时) | 完成时间取最后一个审批节点的动作时间 |
| 申请人 / 申请人UserID / 申请人部门 | 姓名与部门来自通讯录接口，受可见范围限制 |
| 金额合计 | 顶层金额控件合计（明细控件中的金额单独归集，避免重复计算） |
| 表单内容 | 全部控件值转为可读文本，含明细控件逐行展开 |
| 审批流程 | 节点序号、审批方式（或签/会签）、审批人、意见与时间 |
| 备注 / 审批节点数 / 附件数 / 抄送人 | 审批过程中的补充信息 |

XLSX 导出为多工作表：`审批明细` / `按模板统计` / `按状态统计` / `按申请人统计` / `金额汇总`。

### 分析维度

状态分布、模板维度（通过率 / 平均时长 / 金额）、申请人 Top N、按日与按月提交量、
审批时长（均值 / 中位数 / 最长 / 最短）、按字段的金额汇总，以及派生结论
（整体通过率、通过率最低的模板、耗时最长的模板、超期未审批的单据等）。

### 接口限制（已在服务端层自动处理）

| 限制 | 处理方式 |
| :--- | :--- |
| `getapprovalinfo` 时间跨度 ≤ 31 天 | 自动按 31 天切窗后逐窗拉取 |
| 单次最多 100 条 | 自动游标分页 |
| 600 次/分钟 | 详情拉取并发数由 `WECOM_APPROVAL_CONCURRENCY` 控制（默认 4） |
| 同企业同接口全局并发上限（`45033`） | 并发数保持低位；命中 `45033` 时自动退避重试，仍失败则在 `notes` 中显式报告缺失条数 |
| 单条详情失败 | 记录错误并跳过，不影响整批导出 |

## ⚙️ Supervisor 守护进程部署

服务由**系统级 supervisord** 统一管理，配置文件位于 `/usr/local/etc/supervisor.d/wecom-mcp-server.conf`。

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

常用命令：

```bash
supervisorctl status wecom-mcp-server   # 查看状态
supervisorctl restart wecom-mcp-server  # 重启（改完 .env 后必须执行）
supervisorctl stop wecom-mcp-server     # 停止
supervisorctl reread && supervisorctl update  # 配置变更后重载
```

### 配置要点

| 项 | 值 | 说明 |
| :--- | :--- | :--- |
| 启动命令 | `scripts/run_mcp_server.sh` | 启动包装脚本，负责清理端口残留实例后再拉起服务（见下方说明） |
| `autostart` | `true` | 随系统启动自动拉起 |
| 监听地址 | `127.0.0.1` | MCP 协议**无鉴权**，绑定 `0.0.0.0` 会让同网段任意主机调用企业微信 API |
| `PATH` | 需含 `/usr/sbin` | macOS 上 `lsof` 位于 `/usr/sbin/lsof`，缺失会导致包装脚本的端口检测静默失效 |
| 出网策略 | `WECOM_API_TRUST_ENV=false` + `no_proxy=*` | **必设**，见下方说明 |
| `.env` 变更 | 需 `restart` | `.env` 由 `pydantic-settings` 在进程启动时读取 |

### ⚠️ 为什么需要启动包装脚本

当 supervisord 被 `SIGKILL`（非优雅停止）时，它派生的子进程**不会随之退出**，会变成孤儿进程继续占用 TCP 端口。此时新的 supervisord 实例拉起服务会因 `address already in use` 陷入重启循环——服务表面显示 `STARTING`，实际永远起不来。

`scripts/run_mcp_server.sh` 在启动前完成两件事：

1. 用 `lsof` 找出占用目标端口的进程，用 PID 文件交叉验证，**只清理属于本项目**的实例（工作目录匹配或 PID 文件登记），绝不误杀同端口的其他服务；
2. 写入 `data/run/mcp_server.pid`，供下次启动时快速识别残留。

脚本通过 `exec` 启动真正的服务进程，因此 supervisord 记录的 PID 依然是服务本身，`stop`/`restart` 行为不受影响。

### ⚠️ 为什么守护进程要关掉代理继承

`httpx` 默认读取 `HTTP_PROXY` / `HTTPS_PROXY` 环境变量。守护进程继承的是**启动它的那一次会话**的环境——若该代理来自 IDE 沙箱等临时组件，会随会话结束而失效，届时服务将持续报 `All connection attempts failed`，而同一时刻在终端里执行却一切正常。

因此配置中显式设置 `WECOM_API_TRUST_ENV=false` 并清空 `no_proxy`，使服务直连企业微信 API，不与任何临时会话耦合。若生产环境确实需要走代理，请改为在 `environment` 中指定一个稳定的代理地址。

### 接入 MCP 客户端

- **streamable-http**（本部署默认）：客户端配置 `{"type": "http", "url": "http://127.0.0.1:8101/mcp"}`
- **stdio**：无需 supervisord，由 MCP 客户端自行拉起子进程；此时 `WECOM_API_TRUST_ENV` 应保持 `true`（或直接不设置代理变量）

### 单实例约束

消息库与审批库（`data/*.db`）是本地 SQLite 文件，**只能由一个进程写入**。请勿同时运行 supervisord 实例与手工启动的进程，否则可能触发数据库锁冲突。

启动包装脚本会自动清理**属于本项目**的端口残留实例，但不会干预你手工启动的其他进程——若手工实例正在运行，托管实例的日志会给出提示并绑定失败。

## 📁 项目结构

```
wecom-mcp-server/
├── src/
│   └── wecom_mcp_server/
│       ├── __init__.py
│       ├── server.py            # MCP Server 入口，工具注册
│       ├── config.py            # 配置管理 (pydantic-settings)
│       ├── aes.py               # 纯 Python AES-256-CBC 实现
│       ├── crypto.py            # 企业微信消息加解密逻辑
│       ├── wecom_client.py      # 企业微信 API 异步客户端（含审批开放接口）
│       ├── webhook.py           # 接收消息的 Webhook 服务
│       ├── storage.py           # SQLite 消息持久化与文件管理
│       ├── approval.py          # 审批领域层：控件解析、流程解析、采集服务
│       ├── approval_storage.py  # 审批数据 SQLite 持久化与查询
│       ├── approval_export.py   # 审批导出 (CSV/XLSX/JSON) 与统计分析
│       └── xlsx_writer.py       # 零依赖 XLSX 写出器
├── tests/
│   ├── fixtures/                # 审批报文样例
│   ├── test_approval.py         # 审批功能离线测试（无需企业微信凭据）
│   ├── test_mcp_live.py         # MCP 协议级联调测试（streamable-http）
│   └── test_mcp_stdio.py        # MCP 协议级联调测试（stdio）
├── scripts/
│   └── run_mcp_server.sh        # supervisord 启动包装脚本（清理端口残留实例）
├── .env.example                 # 环境变量示例
├── pyproject.toml               # 项目依赖与构建配置
└── README.md                    # 英文文档 (默认)
└── README.zh-CN.md              # 本文档
```

## 🔑 配置说明

### 企业微信应用权限

1. **基础发送权限**：需在企业微信后台创建自建应用，并获取 `CorpID`, `Secret`, `AgentId`。
2. **通讯录读取权限**（可选）：在"管理工具 → 通讯录同步"中开启 `API接口同步`，获取 `contacts_secret`。如不配置，仅能读取应用可见范围内的成员。
3. **接收消息**（可选）：在应用的"接收消息"设置中开启 API 接收，获取 `Token` 和 `EncodingAESKey`。本地开发时需使用内网穿透工具（如 ngrok, cloudflared）暴露回调地址。
4. **审批数据权限**：在"应用管理 → 审批 → API"中把自建应用加入「可调用应用」，并授予「审批数据权限」，详见上文[审批功能](#-审批功能工作台---审批)。

### 数据存储结构

```
data/
├── wecom_messages.db          # SQLite 数据库 (消息，按年分表)
├── wecom_approvals.db         # SQLite 数据库 (审批单 + 模板元信息)
├── exports/                   # 审批导出文件 (csv / xlsx / json)
└── files/
    ├── 2026/
    │   ├── 07/
    │   │   ├── image_xxx.jpg
    │   │   └── report.pdf
    │   └── ...
    └── ...
```

### 运行测试

```bash
uv run python tests/test_approval.py
```

## 📜 License

[MIT](LICENSE)
