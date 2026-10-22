#!/bin/sh
# 终端里执行：./启动后端.sh
# 只起**后端**（C 端，用户对话）。管理后台是 ./启动后台.sh，两个是独立进程。
cd "$(dirname "$0")"
ROOT="$PWD"

PORT="${PATTERNFLOW_PORT:-8787}"

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
  exit 1
fi

if curl -s --noproxy '*' --max-time 2 "http://127.0.0.1:$PORT/api/health" >/dev/null 2>&1; then
  echo "后端已在运行，直接打开页面…"
  open "http://127.0.0.1:$PORT/" 2>/dev/null || true
  exit 0
fi

echo "启动后端（python: $PY  端口: $PORT）…"
cd "$ROOT/后端"
exec "$PY" server.py --port "$PORT" --open "$@"
