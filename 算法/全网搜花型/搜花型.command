#!/bin/bash
# 全网搜花型 · 双击即用
#
# 这个脚本只是把 run.py 包了一层问答 —— 你不用记任何参数：
#   问需求 → 选路线 → 给个目录名 → 跑 → 自动画好画廊页并打开。
#
# 想自己拼参数（换模型、严判工艺、只跑判定…）就直接跑 run.py，
# 用法写在 README.md 里。
#
# 环境变量：
#   PF_DRYRUN=1   只打印要执行的命令，不真跑（排查用）

cd "$(dirname "$0")" || exit 1
ROOT="$PWD"
PY="$ROOT/.venv/bin/python"

if [ ! -x "$PY" ]; then
  echo "环境还没装：找不到 $PY"
  echo "先双击同目录的 安装.command，装完再来。"
  read -r _ ; exit 1
fi

echo "=============== 全网搜花型 ==============="
echo
printf "选路线 [1/2]: "
read -r ROUTE
[ -n "$ROUTE" ] || { ROUTE=2; echo; echo "  没选，按路线 2（宁缺毋滥）走。"; }

case "$ROUTE" in
  1) printf "要多少张: "
     read -r WANT
     [ -n "$WANT" ] || { WANT=100; echo; echo "  没填，按 100 张。"; }
     SUB="search"
     ARGS="--want $WANT"
     TAG="路线1-凑$WANT张"
     REFILL_WANT="$WANT"
     ;;
  2) SUB="pick"
     ARGS="--want 0"
     TAG="路线2-不凑数"
     REFILL_WANT="100"      # 路线 2 自己不凑数，但给已有目录补图时要有个目标
     ;;
  *) echo "只能填 1 或 2。" ; read -r _ ; exit 1 ;;
esac

echo
printf "需求: "
read -r NEED
if [ -z "$NEED" ]; then
  echo "需求不能为空。"
  read -r _ ; exit 1
fi

DEF="产物/out_$(date +%m%d_%H%M)"
echo
printf "输出目录: "
read -r OUT
if [ -z "$OUT" ]; then
  OUT="$DEF"
  echo
  echo "  用默认：$OUT"
fi

if [ -f "$ROOT/$OUT/index.json" ]; then
  echo
  echo "这个目录里已经有产物了（$OUT/index.json）。"
  echo "脚本只往空目录里落，不会覆盖已有的 —— 换一个名字重跑；"
  echo "要给已有目录补图，请用命令："
  echo "  ./.venv/bin/python run.py refill \"$NEED\" --outdir $OUT --want $REFILL_WANT"
  read -r _ ; exit 1
fi

echo
echo "------------------------------------------"
echo "  需求  $NEED"
echo "  路线  $TAG"
echo "  目录  $ROOT/$OUT"
echo "  日志  $OUT/_run.log"
echo "------------------------------------------"
echo

if [ "$PF_DRYRUN" = "1" ]; then
  echo "[dry-run] 将执行："
  echo "  $PY run.py $SUB $ARGS \"$NEED\" --outdir $OUT"
  exit 0
fi

mkdir -p "$ROOT/$OUT"
set -o pipefail
"$PY" run.py $SUB $ARGS "$NEED" --outdir "$OUT" 2>&1 | tee "$ROOT/$OUT/_run.log"
code=$?

echo
if [ "$code" -ne 0 ]; then
  echo "没跑成（退出码 $code）。看上面 $OUT/_run.log 的报错。"
  read -r _ ; exit "$code"
fi

N=$("$PY" -c "import json,sys;print(json.load(open(sys.argv[1]))['count'])" \
     "$ROOT/$OUT/index.json" 2>/dev/null || echo 0)
echo "=========================================="
echo "  落了 $N 张。"
if [ "${N:-0}" -gt 0 ] 2>/dev/null; then
  "$PY" tools/make_gallery.py --outdir "$OUT" --side 320 --title "$NEED" \
      | tail -1
  if [ "$PF_NOOPEN" = "1" ]; then
    echo "  （PF_NOOPEN=1，没自动打开）"
  else
    echo "  正在打开画廊页…"
    open "$ROOT/$OUT/_gallery.html"
  fi
else
  echo "  一张都没筛出来。换个说法再试一次（越具体越好，"
  echo "  比如把「花」换成「牡丹 荷花」，把「复古」换成「青花」）。"
fi
echo "  原图在 $OUT/orig/，索引 $OUT/index.json"
echo "=========================================="
echo
read -r _
