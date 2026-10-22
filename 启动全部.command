#!/bin/zsh
# 双击即可启动（macOS 会用「终端」打开）。一次把两个进程都拉起来，
# 并且等它们就绪后自动在浏览器里打开两个页面：
#   后端（C 端对话）  http://127.0.0.1:8787/  —— 用户聊天用的
#   后台（B 端运营）  http://127.0.0.1:8788/  —— 运营用的，要口令
# 两个是独立进程：后台挂了用户照样能聊，反过来也一样。
# 停：在窗口里按 Ctrl+C，两个一起停（关窗口也一样）。
cd "$(dirname "$0")"
ROOT="$PWD"

PORT="${PATTERNFLOW_PORT:-8787}"
APORT="${PATTERNFLOW_ADMIN_PORT:-8788}"

# 本机可能配了系统代理，curl 会把 127.0.0.1 的请求也送进代理绕一圈，
# 于是"服务明明在跑却探测不到"→ 重复启动 → 端口冲突。一律绕开代理。
probe(){
  curl -s --noproxy '*' --max-time "$1" "http://127.0.0.1:$2/api/health" >/dev/null 2>&1
}

PY=""
for c in "${PATTERNFLOW_PYTHON:-}" \
         "/usr/bin/python3" \
         "/opt/homebrew/bin/python3" \
         "python3"; do
  [ -n "$c" ] || continue
  if [ -x "$c" ] || command -v "$c" >/dev/null 2>&1; then PY="$c"; break; fi
done

if [ -z "$PY" ]; then
  echo "找不到 python3，请先安装 Python 3.9+。"
  read -r "?按回车关闭…"
  exit 1
fi

cleanup(){
  echo ""
  echo "正在停两个服务…"
  [ -n "$PID_C" ] && kill "$PID_C" 2>/dev/null
  [ -n "$PID_A" ] && kill "$PID_A" 2>/dev/null
  sleep 1
  exit 0
}
trap cleanup INT TERM

echo "python: $PY"
echo ""

if probe 2 "$PORT"; then
  echo "  后端 8787 已在运行，跳过。"
else
  echo "  启动后端      http://127.0.0.1:$PORT/"
  ( cd "$ROOT/后端" && exec "$PY" server.py --port "$PORT" ) &
  PID_C=$!
fi

if probe 2 "$APORT"; then
  echo "  后台 8788 已在运行，跳过。"
else
  echo "  启动后台      http://127.0.0.1:$APORT/"
  ( cd "$ROOT/后台" && exec "$PY" admin_server.py --port "$APORT" ) &
  PID_A=$!
fi

# 等两个都起来再开浏览器，否则点进去是白页
i=0
while [ $i -lt 60 ]; do
  probe 1 "$PORT" && probe 1 "$APORT" && break
  i=$((i+1))
  sleep 0.25
done

echo ""
echo "两个服务都起来了："
echo "  后端（对话）    http://127.0.0.1:$PORT/"
echo "  后台（运营）    http://127.0.0.1:$APORT/"
echo ""
echo "关掉这个窗口或按 Ctrl+C，两个一起停。"
# 两个页面都开，省得自己敲地址（后台要点进去输口令）
open "http://127.0.0.1:$PORT/"  2>/dev/null || true
open "http://127.0.0.1:$APORT/" 2>/dev/null || true

wait
