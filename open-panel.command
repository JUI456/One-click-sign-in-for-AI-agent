#!/bin/bash
# 一键打开签到面板（轻量模式 · 关标签页即停）
# 用法：双击本文件即可。若面板已在运行则直接打开浏览器；否则以“关标签页即停”的轻量模式
#      启动并打开浏览器。停止方式：关闭/隐藏网页约 1 分钟后服务自动退出；或在网页里点「⏻ 停止服务」。
#      注意：刷新网页不会误杀服务（重载间隙很短，心跳会立即重新上报）；只有真正关掉标签页才会停止。
DIR="$(cd "$(dirname "$0")" && pwd)"
PY=/opt/homebrew/bin/python3
PORT=8787

if curl -s -o /dev/null --max-time 2 "http://127.0.0.1:$PORT/"; then
  echo "面板已在运行，直接打开浏览器"
  open "http://127.0.0.1:$PORT/"
  exit 0
fi

echo "启动面板（轻量模式 · 关标签页即停，空闲 1 分钟自动关闭）…"
nohup "$PY" "$DIR/server.py" --idle-shutdown 1 >/tmp/checkin-panel-server.log 2>&1 &
sleep 1
open "http://127.0.0.1:$PORT/"
