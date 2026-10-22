#!/bin/sh
# 自动打标签 · 环境安装
#
# 这个脚本做三件事：
#   1. 建一个只属于这个算法的 .venv，装上 Pillow；
#   2. 看看本机的推理服务起没起；
#   3. 没起就告诉你怎么起（双击 本地模型/ 里那两个脚本就行）。
#
# 为什么算法要自己一份 venv
#     PatternFlow 后端是刻意零依赖的（只用标准库）。这个算法要 Pillow，
#     直接装进后端解释器等于把"要不要装第三方包"扩散到整个服务 ——
#     一次装坏，整个产品起不来。所以算法带自己的环境，后端按子进程调用。
#
# 为什么推理不装在这里
#     视觉大模型动辄几个 GB，还要 mlx / torch 那一大套。把它塞进算法环境
#     是自找麻烦。所以推理单独一个目录（本地模型/）托管，算法这边只管发
#     HTTP 请求 —— 这里只装一个 Pillow。
#
# 用法：双击，或者在终端里执行  ./安装.command

cd "$(dirname "$0")" || exit 1
ROOT="$PWD"
ENGINE="$ROOT/本地模型"
OWN="http://127.0.0.1:11435"
OLLAMA="http://127.0.0.1:11434"

echo "自动打标签 · 安装"
echo "  目录：$ROOT"
echo

# ---------- 1. 自带 venv ----------
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
  "$PY" -m venv .venv || { echo "建 venv 失败。"; read -r _ ; exit 1; }
fi

if "$ROOT/.venv/bin/python" -c "import PIL" >/dev/null 2>&1; then
  echo "依赖已经在了，跳过下载。"
else
  echo "下载 Pillow…"
  "$ROOT/.venv/bin/pip" install --quiet --disable-pip-version-check \
    -r requirements.txt || { echo "装 Pillow 失败，检查网络。"; read -r _ ; exit 1; }
fi

"$ROOT/.venv/bin/python" - <<'PY'
import PIL
print("  搞定：Pillow %s" % PIL.__version__)
PY
echo

# ---------- 2. 看推理服务 ----------
alive() { curl -s --noproxy '*' --max-time 3 "$1/v1/models" >/dev/null 2>&1; }

echo "检查本地推理服务…"
if alive "$OWN"; then
  echo "  自带引擎已经在跑（$OWN），齐了。"
elif alive "$OLLAMA"; then
  echo "  Ollama 在跑（$OLLAMA）。跑打标时带上它："
  echo "      ./.venv/bin/python run.py tag ./inputs --base-url $OLLAMA/v1 \\"
  echo "          --model qwen3.5:4b"
else
  echo "  没有服务在跑。"
  echo
  if [ -x "$ENGINE/.venv/bin/python" ]; then
    echo "  自带引擎已经装好，直接双击启动就行："
    echo "      本地模型/启动本地模型.command"
  else
    echo "  本机自带的引擎还没装（它就是给这个算法配的推理端）："
    echo "      1) 双击  本地模型/安装.command        装引擎，第一次几分钟"
    echo "      2) 双击  本地模型/启动本地模型.command  下模型（约 3 GB）并起服务"
  fi
  echo
  echo "  如果你更习惯 Ollama，也完全可以："
  echo "      ollama serve && ollama pull qwen3.5:4b"
  echo "      跑的时候加  --base-url http://127.0.0.1:11434/v1 --model qwen3.5:4b"
fi
echo
echo "  推荐模型：Qwen3.5-4B（自带引擎默认带的就是它）。"
echo "  想看完整对照：./.venv/bin/python run.py recommend"

echo
echo "装完了。试试："
echo "    ./.venv/bin/python run.py probe          # 再查一遍环境"
echo "    ./.venv/bin/python run.py tag ./inputs   # 给 inputs 里的图打标"
echo
echo "（主程序调用的是 .venv/bin/python -m atag.service，所以推理服务得开着）"
