#!/bin/bash
# 一键打开签到面板（轻量模式）
# 用法：双击本文件即可。若面板已在运行则直接打开浏览器；否则以“空闲 30 分钟自动关闭”的
#      轻量模式启动并打开浏览器。停止方式：关闭网页后约 30 分钟自动关，或在网页里点「⏻ 停止服务」。
DIR="$(cd "$(dirname "$0")" && pwd)"
PY=/opt/homebrew/bin/python3
PORT=8787

if curl -s -o /dev/null --max-time 2 "http://127.0.0.1:$PORT/"; then
  echo "面板已在运行，直接打开浏览器"
  open "http://127.0.0.1:$PORT/"
  exit 0
fi

echo "启动面板（轻量模式，空闲 30 分钟自动关闭）…"
nohup "$PY" "$DIR/server.py" --idle-shutdown 30 >/tmp/checkin-panel-server.log 2>&1 &
sleep 1
open "http://127.0.0.1:$PORT/"
