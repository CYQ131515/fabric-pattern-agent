#!/bin/zsh
# 花型生成运行环境安装（numpy + Pillow）
#
# 为什么要这一步：
#   pattern_studio 需要 numpy + Pillow，但主程序是零依赖的，所以它单独跑子进程。
#   以前是每次用 `uv run --with numpy --with pillow` 临时解析依赖 —— 外网一断
#   （PyPI 不通 / 代理挂了），uv 会卡在解析上，表现为：
#       花型生成没跑通（退出码 2）。cause: operation timed out
#   这个脚本在算法目录建一个固定 .venv，之后后端直接用它起进程，**零网络**。
#
# 用法：双击本文件，或在终端执行 ./安装.command
set -e
cd "$(dirname "$0")"

VENV=".venv"
PY=""
# 1) 优先用本机已装的 Python 3.13（workbuddy / uv 管理的都行）
for cand in \
  "$HOME/.workbuddy/binaries/python/versions/3.13.12/bin/python3" \
  "$(command -v python3.13 2>/dev/null || true)" \
  "$(command -v python3 2>/dev/null || true)"
do
  if [ -n "$cand" ] && [ -x "$cand" ]; then PY="$cand"; break; fi
done
if [ -z "$PY" ]; then
  echo "找不到 Python 3，请先装 Python 3.12+。"
  exit 1
fi

if [ -x "$VENV/bin/python" ] && "$VENV/bin/python" -c "import numpy, PIL" >/dev/null 2>&1; then
  echo "运行环境已就绪：$VENV"
  "$VENV/bin/python" -c "import sys, numpy, PIL; print('  Python', sys.version.split()[0], '· numpy', numpy.__version__, '· Pillow', PIL.__version__)"
  exit 0
fi

echo "创建 $VENV（$PY）…"
rm -rf "$VENV"
"$PY" -m venv "$VENV"

# 2) 优先从同项目的「分层 PSD」venv 里拷贝依赖（离线也能装）
SP="../psd分层/.venv/lib/python3.13/site-packages"
DEST="$VENV/lib/python3.13/site-packages"
if [ -d "$SP/numpy" ] && [ -d "$SP/PIL" ]; then
  echo "从同项目依赖离线复制 numpy / Pillow …"
  cp -a "$SP/numpy" "$DEST/" 2>/dev/null || true
  cp -a "$SP/numpy.libs" "$DEST/" 2>/dev/null || true
  cp -a "$SP/PIL" "$DEST/" 2>/dev/null || true
  cp -a "$SP/Pillow.libs" "$DEST/" 2>/dev/null || true
  cp -a "$SP"/numpy-*.dist-info "$DEST/" 2>/dev/null || true
  cp -a "$SP"/pillow-*.dist-info "$DEST/" 2>/dev/null || true
fi

# 3) 还缺就联网装一次（有网时）
if ! "$VENV/bin/python" -c "import numpy, PIL" >/dev/null 2>&1; then
  echo "本地没有可复制的依赖，联网安装 …"
  "$VENV/bin/python" -m pip install --quiet --upgrade pip >/dev/null 2>&1 || true
  "$VENV/bin/python" -m pip install numpy pillow
fi

"$VENV/bin/python" -c "import sys, numpy, PIL; print('完成：Python', sys.version.split()[0], '· numpy', numpy.__version__, '· Pillow', PIL.__version__)"
echo "之后后端会自动使用 $PWD/$VENV"
