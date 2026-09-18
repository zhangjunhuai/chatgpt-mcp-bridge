#!/usr/bin/env bash
# 对 MCP 连接器 URL 做三步自测:握手 → 拉工具列表 → 真实执行一条命令
# 用法: ./test.sh https://xxx.trycloudflare.com/mcp/<token>
URL="${1:?用法: ./test.sh <连接器URL>}"
H1='Content-Type: application/json'
H2='Accept: application/json, text/event-stream'

echo "== 1) initialize(握手)=="
S=$(curl -s -m 25 -D - -o /dev/null -X POST "$URL" -H "$H1" -H "$H2" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"test","version":"0"}}}' \
  | grep -i mcp-session-id | tr -d '\r' | awk '{print $2}')
if [ -n "$S" ]; then echo "OK  session=$S"; else echo "FAILED: 服务器没响应,检查服务是否在跑"; exit 1; fi

echo "== 2) tools/list(应列出 13 个工具)=="
curl -s -m 25 -X POST "$URL" -H "$H1" -H "$H2" -H "Mcp-Session-Id: $S" \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' | grep -oE '"name":"[a-z_]+"' | sort -u

echo "== 3) tools/call(真实执行 hostname)=="
curl -s -m 25 -X POST "$URL" -H "$H1" -H "$H2" -H "Mcp-Session-Id: $S" \
  -d '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"run_command","arguments":{"argv":["hostname"]}}}' \
  | grep '^data:' | head -c 300
echo
echo "三步都 OK = 链路全通,ChatGPT 那边一定连得上"
