#!/bin/sh
# 四方连续引擎 · 依赖安装（numpy + Pillow）
#
# 什么时候要跑这个
#     织印师的四方连续功能报「引擎还没装好」时跑一次即可。装完不用重启后端，
#     直接重试就行。
#
# 为什么算法要自己一份 venv
#     PatternFlow 后端是刻意零依赖的（只用标准库，连 JPEG 解码都是自己调 sips
#     转 PNG 再自研解码）。四方连续要 numpy + Pillow，如果直接装进后端的
#     解释器，等于把"要不要装第三方包"这件事扩散到整个服务 —— 一次装坏，
#     整个产品起不来。所以算法带自己的环境，后端按子进程调用它。
#
# 用法：双击，或者在终端里执行  ./安装.command
set -e

cd "$(dirname "$0")" || exit 1
ROOT="$PWD"

echo "四方连续引擎 · 安装依赖"
echo "  目录：$ROOT"
echo

# 挑一个能建 venv 的 python（和后端启动脚本挑法保持一致，优先带 venv 模块的）
PY=""
for c in "${PATTERNFLOW_PYTHON:-}" \
         "$HOME/.workbuddy/binaries/python/versions/3.13.12/bin/python3" \
         "/opt/homebrew/bin/python3" \
         "/usr/local/bin/python3" \
         "/usr/bin/python3" \
         "python3"; do
  [ -n "$c" ] || continue
  if [ -x "$c" ] || command -v "$c" >/dev/null 2>&1; then
    if "$c" -c "import venv" >/dev/null 2>&1; then PY="$c"; break; fi
  fi
done

if [ -z "$PY" ]; then
  echo "找不到带 venv 模块的 python3。装一个 Python 3.9+ 再来。"
  read -r _ ; exit 1
fi
echo "用 $PY 建环境…"

if [ ! -x "$ROOT/.venv/bin/python" ]; then
  "$PY" -m venv .venv
fi

# 已经装好就不用再连网了 —— 装过一次之后这个脚本应该秒过
if "$ROOT/.venv/bin/python" -c "import numpy, PIL" >/dev/null 2>&1; then
  echo "依赖已经在了，跳过下载。"
else
  echo "下载 numpy 和 Pillow…"
  "$ROOT/.venv/bin/pip" install --quiet --disable-pip-version-check \
    -r requirements.txt
fi

echo
"$ROOT/.venv/bin/python" - <<'PY'
import numpy, PIL
print("搞定：numpy %s · Pillow %s" % (numpy.__version__, PIL.__version__))
PY
echo
echo "可以了。回到对话里重试四方连续就行，后端不用重启。"
echo "（自检：后端 /api/health 里的 seamless.available 应该是 true）"
