#!/bin/sh
# 终端里执行：./启动后台.sh
# 只起**管理后台**（B 端，运营用，要口令）。用户对话是 ./启动后端.sh。
cd "$(dirname "$0")"
ROOT="$PWD"

PORT="${PATTERNFLOW_ADMIN_PORT:-8788}"

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
  echo "后台已在运行，直接打开页面…"
  open "http://127.0.0.1:$PORT/" 2>/dev/null || true
  exit 0
fi

echo "启动管理后台（python: $PY  端口: $PORT）…"
cd "$ROOT/后台"
exec "$PY" admin_server.py --port "$PORT" --open "$@"
