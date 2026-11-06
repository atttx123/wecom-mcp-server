#!/bin/bash
# WeCom MCP Server 启动包装脚本
#
# 背景：supervisord 被 SIGKILL（非优雅停止）时，它派生的子进程不会随之退出，
#       会变成孤儿进程继续占用 TCP 端口。此时新的 supervisord 实例拉起服务会因
#       "address already in use" 陷入重启循环。本脚本在启动前清理属于本项目的
#       残留实例，保证服务总能成功绑定端口。
#
# 关键点：
#   1. lsof 在 macOS 上位于 /usr/sbin/lsof，而 supervisor 的 environment PATH
#      通常只含 /usr/local/bin:/usr/bin:/bin —— 必须用绝对路径，否则静默失效。
#   2. 双重检测：端口占用者 (lsof) + PID 文件，交叉验证后清理。
#   3. 只清理「工作目录为本项目」或「PID 文件记录」的进程，避免误杀他人服务。
set -u

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PORT="${WECOM_MCP_PORT:-8101}"
HOST="${WECOM_MCP_HOST:-127.0.0.1}"
RUN_DIR="$PROJECT_DIR/data/run"
PID_FILE="$RUN_DIR/mcp_server.pid"

log() { echo "[run_mcp_server] $*"; }

# ---- 定位 lsof 绝对路径 (服务环境 PATH 通常不含 /usr/sbin) ----
LSOF=""
for candidate in /usr/sbin/lsof /usr/bin/lsof /bin/lsof "$(command -v lsof 2>/dev/null || true)"; do
  if [ -n "$candidate" ] && [ -x "$candidate" ]; then
    LSOF="$candidate"
    break
  fi
done
if [ -z "$LSOF" ]; then
  log "警告: 未找到 lsof，端口残留检测能力受限"
fi

# ---- 判断某 pid 是否属于本项目 ----
_is_ours() {
  local pid="$1"
  # 条件 1: PID 文件登记过
  if [ -f "$PID_FILE" ] && [ "$(cat "$PID_FILE" 2>/dev/null)" = "$pid" ]; then
    return 0
  fi
  # 条件 2: 工作目录为本项目
  if [ -n "$LSOF" ]; then
    local cwd
    cwd="$("$LSOF" -a -p "$pid" -d cwd -Fn 2>/dev/null | grep '^n' | cut -c2-)"
    if [ "$cwd" = "$PROJECT_DIR" ]; then
      return 0
    fi
  fi
  return 1
}

# ---- 收集候选残留进程 ----
CANDIDATES=""
if [ -n "$LSOF" ]; then
  CANDIDATES="$("$LSOF" -nP -iTCP:"$PORT" -sTCP:LISTEN -t 2>/dev/null || true)"
fi
if [ -f "$PID_FILE" ]; then
  CANDIDATES="$CANDIDATES $(cat "$PID_FILE" 2>/dev/null || true)"
fi

seen=""
for pid in $CANDIDATES; do
  [ -n "$pid" ] || continue
  case " $seen " in *" $pid "*) continue ;; esac
  seen="$seen $pid"
  kill -0 "$pid" 2>/dev/null || continue

  if _is_ours "$pid"; then
    log "发现残留实例占用端口 $PORT (pid=$pid)，正在清理"
    kill -TERM "$pid" 2>/dev/null || true
    for _ in 1 2 3 4 5 6 7 8 9 10; do
      kill -0 "$pid" 2>/dev/null || break
      sleep 0.3
    done
    if kill -0 "$pid" 2>/dev/null; then
      kill -KILL "$pid" 2>/dev/null || true
      sleep 0.5
    fi
    log "残留实例 pid=$pid 已清理"
  else
    log "端口 $PORT 被非本项目进程占用 (pid=$pid)，不处理，请人工确认"
  fi
done

# ---- 清理 PID 文件并登记新实例 ----
mkdir -p "$RUN_DIR"
rm -f "$PID_FILE"
echo $$ > "$PID_FILE"

log "启动 wecom-mcp-server: http://$HOST:$PORT/mcp (pid=$$)"
exec "$PROJECT_DIR/.venv/bin/wecom-mcp-server" \
  --transport streamable-http \
  --host "$HOST" \
  --port "$PORT"
