#!/bin/zsh
# 构建并安装到「应用程序」，让 Launchpad / Spotlight 能搜到。
# 用法：双击本文件，或在终端执行 ./安装到应用程序.command
set -e
cd "$(dirname "$0")"

APP_NAME="PatternFlow.app"
DEST="/Applications/PatternFlow 花型智造.app"

echo "==> 先构建最新版本"
./构建.command

echo "==> 安装到 /Applications"
rm -rf "$DEST"
cp -R "$APP_NAME" "$DEST"
xattr -dr com.apple.quarantine "$DEST" 2>/dev/null || true

echo "==> 注册到 LaunchServices / Spotlight"
/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister -f "$DEST" 2>/dev/null || true
mdimport "$DEST" 2>/dev/null || true

# Launchpad 会缓存应用列表，重启 Dock 让它立刻刷新
killall Dock 2>/dev/null || true

echo "==> 完成：$DEST"
echo "    Spotlight / Launchpad 搜「PatternFlow」或「花型智造」即可。"
echo "    以后改完代码，重新跑一次本脚本即可更新安装版。"
