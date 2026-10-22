#!/bin/zsh
# 构建 PatternFlow 桌面端（macOS 原生 WKWebView 壳）
# 用法：双击本文件，或在终端执行 ./构建.command
set -e
cd "$(dirname "$0")"

APP="PatternFlow.app"
BIN="$APP/Contents/MacOS/PatternFlow"

echo "==> 清理旧构建"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

echo "==> 编译 Swift（首次约 10~20 秒）"
# 本机 CommandLineTools 的 Swift 6.3 配 MacOSX27 SDK 会报模块不匹配；
# 自动挑一个和编译器同代的 SDK（26.x 优先），没有就用默认。
SDKROOT=""
for s in MacOSX26.5.sdk MacOSX26.sdk MacOSX15.4.sdk; do
  if [ -d "/Library/Developer/CommandLineTools/SDKs/$s" ]; then
    SDKROOT="/Library/Developer/CommandLineTools/SDKs/$s"; break
  fi
done
if [ -n "$SDKROOT" ]; then
  echo "    SDK: $SDKROOT"
  swiftc -sdk "$SDKROOT" -target arm64-apple-macos26.0 -O Sources/main.swift -o "$BIN" \
    -framework AppKit -framework WebKit
else
  swiftc -O Sources/main.swift -o "$BIN" -framework AppKit -framework WebKit
fi

echo "==> 写入 Info.plist"
cp Info.plist "$APP/Contents/Info.plist"

if [ -f "Resources/AppIcon.icns" ]; then
  cp "Resources/AppIcon.icns" "$APP/Contents/Resources/AppIcon.icns"
fi

echo "==> 临时签名（ad-hoc）"
codesign --force --deep --sign - "$APP" >/dev/null 2>&1 || true

echo "==> 完成：$PWD/$APP"
echo "    双击打开即可；Web 端仍可照常在浏览器访问 http://127.0.0.1:8787/"
