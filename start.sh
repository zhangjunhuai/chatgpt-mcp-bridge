#!/usr/bin/env bash
# 启动 MCP server。公网入口是 Tailscale Funnel(持久配置,开机自动生效),无需任何隧道进程。
# 用法: ./start.sh              复用 token.txt 里的 token(首次自动生成)
#       ./start.sh <newtoken>   强制换 token(同时更新 token.txt,需同步更新 ChatGPT 连接器)
set -e
cd "$(dirname "$0")"
PORT=8000
SERVER="server_v2.2.py"
UNIT="chatgpt-mcp-bridge.service"
TOKEN_FILE="$(dirname "$0")/token.txt"
if [ -n "$1" ]; then TOKEN="$1"; echo "$TOKEN" > "$TOKEN_FILE"
elif [ -f "$TOKEN_FILE" ]; then TOKEN="$(cat "$TOKEN_FILE")"
else TOKEN="$(python3 -c 'import secrets;print(secrets.token_urlsafe(24))')"; echo "$TOKEN" > "$TOKEN_FILE"; fi
# miniconda base 的 python:mcp 模块装在那里,实验也跑在 conda base
PY="$HOME/miniconda3/bin/python3"
[ -x "$PY" ] || PY="$(command -v python3)"
# 子进程(所有 run_command)默认继承 conda base 的 PATH:python3/pip/conda 都指向 miniconda
export PATH="$HOME/miniconda3/bin:$PATH"

if systemctl --user cat "$UNIT" >/dev/null 2>&1; then
  # 已安装开机自启服务时，由 systemd 统一托管，避免产生重复进程。
  systemctl --user restart "$UNIT"
  STOP_CMD="systemctl --user stop $UNIT"
else
  # 未安装 systemd 服务时仍可独立使用本脚本。
  pkill -f "server.py --port $PORT" 2>/dev/null || true
  pkill -f "$SERVER --port $PORT" 2>/dev/null || true
  sleep 1
  nohup "$PY" "$SERVER" --port "$PORT" --token-file "$TOKEN_FILE" > server.log 2>&1 &
  STOP_CMD="pkill -f '$SERVER --port $PORT'"
fi

echo "等待服务启动..."
for _ in $(seq 1 15); do
  curl -s -m 2 -o /dev/null "http://127.0.0.1:$PORT/" && break
  sleep 1
done

echo
echo "==== ChatGPT 连接器 URL(固定不变)===="
echo "https://home-mcp.tail4927a2.ts.net/mcp/$TOKEN"
echo "======================================"
echo "前提: tailscaled 在跑(funnel 已持久化)、数据盘已挂载"
echo "     若数据盘没挂载,server 会自动退回 ~/chatgpt-downloads"
echo "服务: $SERVER"
echo "停止: $STOP_CMD"
