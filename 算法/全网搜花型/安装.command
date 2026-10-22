#!/bin/sh
# 全网搜花型 · 环境安装
#
# 这个脚本做两件事：
#   1. 建一个只属于这个算法的 .venv，装上 Pillow；
#   2. 说明搜索来源的情况 —— **百度/360/花瓣/堆糖 免 key，开箱即用**；
#      Patternbank 也免 key，但要另装 Scrapling（可选，不装就跳过）；
#      境外那几家是可选的补充，要 key 的话脚本只能告诉你去哪儿申请。
#
# 为什么算法要自己一份 venv
#     PatternFlow 后端是刻意零依赖的（只用标准库）。这个算法要 Pillow
#     （读图、算接缝、生成缩略图），直接装进后端解释器等于把"要不要装
#     第三方包"扩散到整个服务 —— 一次装坏，整个产品起不来。
#
# 为什么只要 Pillow
#     搜索是 HTTP 请求（标准库 urllib 就够）、需求分析是调大模型
#     （也是 HTTP）、平铺判定全靠 Pillow 的两个整图差分算完。
#     跟别人不同的地方：**这个算法不需要 GPU，也不需要本地模型** ——
#     重活都在云端（搜索站 + 对话模型）那边。
#
# 用法：双击，或者在终端里执行  ./安装.command

cd "$(dirname "$0")" || exit 1
ROOT="$PWD"

echo "全网搜花型 · 安装"
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

# ---------- 2. key 的情况 ----------
echo "搜索来源："
echo "  ✓ 百度图片  免 key，开箱即用（中文花型素材最全）"
echo "  ✓ 360 图片  免 key、免 cookie，开箱即用"
echo "  ✓ 花瓣 / 堆糖  免 key，开箱即用（只认中文检索词）"
# Patternbank 免 key，但靠 Scrapling 的浏览器指纹过反爬 —— 不装就自动跳过，
# 不报错、不影响别家。所以这里要把"装没装"说清楚，别让人以为它坏了。
if "$ROOT/.venv/bin/python" -c "import scrapling" >/dev/null 2>&1; then
  echo "  ✓ Patternbank（专业花型）  免 key，已装 Scrapling，默认在跑"
else
  echo "  ·  Patternbank（专业花型）  免 key，但**没装 Scrapling**，这家会跳过"
  echo "       想开就装：./.venv/bin/pip install \"scrapling[fetchers]\""
  echo "       （专业花型/印花素材库，图本身就是四方连续；只认英文词、量小）"
fi
echo
echo "下面这几家是**可选的补充**（境外来源），不配也能正常跑："
missing=0
check() {  # $1=变量名 $2=名字 $3=申请地址
  eval "v=\$$1"
  if [ -n "$v" ]; then
    echo "  ✓ $2 已配"
  else
    missing=$((missing+1))
    echo "  ·  $2 未配   申请：$3"
  fi
}
check WSEARCH_SERPER_KEY  "Serper"          "https://serper.dev"
check WSEARCH_SERPAPI_KEY "SerpAPI"          "https://serpapi.com"
check WSEARCH_BRAVE_KEY   "Brave 图片搜索"    "https://brave.com/search/api/"
check WSEARCH_PIXABAY_KEY "Pixabay 图库"      "https://pixabay.com/api/docs/"
check WSEARCH_PEXELS_KEY  "Pexels 图库"       "https://www.pexels.com/api/"
check WSEARCH_UNSPLASH_KEY "Unsplash 图库"    "https://unsplash.com/developers"

echo
if [ "$missing" -eq 6 ]; then
  echo "  （六家境外来源一把都没配 —— 没关系，免 key 的百度/360/花瓣/堆糖就够了。"
  echo "    哪天它们被反爬挡了，配一把 Serper（免费 2500 次）就能顶上去："
  echo "       export WSEARCH_SERPER_KEY=你的key）"
fi

echo
echo "  需求分析用的对话模型可以复用主程序已经在用的那把 key（DeepSeek 等）："
echo "      export WSEARCH_LLM_BASE=https://api.deepseek.com/v1"
echo "      export WSEARCH_LLM_KEY=你的key"
echo "      export WSEARCH_LLM_MODEL=deepseek-chat"
echo "  不配也能跑，只是检索词会退化成「关键词 + 行业词模板」，质量差一截。"

echo
echo "装完了。试试："
echo "    ./.venv/bin/python run.py probe                 # 查环境 + 看搜索源状态"
echo "    ./.venv/bin/python run.py keys                  # 各家搜索源的地址"
echo "    ./.venv/bin/python run.py search \"国风 花卉 连衣裙\"   # 真的搜一次"
echo
echo "（主程序调用的是 .venv/bin/python -m wsearch.service，不需要额外装东西）"
