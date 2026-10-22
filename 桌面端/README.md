# PatternFlow 桌面端（macOS）

一个轻量的原生壳：**WKWebView 装现有 Web 界面，启动时自动拉起本地 Python 服务**。
Web 端完全保留，桌面端只是多了一个窗口、菜单和进程守护。

## 打开

已安装到「应用程序」：**PatternFlow 花型智造.app**
Spotlight（⌘Space）或 Launchpad 搜「PatternFlow」/「花型智造」即可打开。

也可以直接双击项目里的 `桌面端/PatternFlow.app`（只影响它自己，不进 Launchpad）。

首次启动会等本地服务起来（几秒）；之后直接打开。

## 它做了什么

- 检查 `127.0.0.1:8787` 上是不是 PatternFlow 后端（只认 `200` 且返回 JSON 带 `"ok"`，不会把别的应用当成后端）。
- 没在跑就用项目里的 Python 启动 `后端/server.py`，并把日志写到
  `~/Library/Logs/PatternFlow/backend-8787.log`。
- 菜单「导航 → 打开管理后台」按需启动 `后台/admin_server.py`；
  如果 8788 被别的应用占用会自动顺延到 8789 / 8790 / 8791（这台机器 8788 被另一个项目占了，实际落在 8790）。
- 退出 App 时，**只关闭自己拉起的进程**；如果你本来就开着 Web 后端，不会被误杀。

## 菜单

| 菜单 | 作用 |
|---|---|
| 刷新页面 ⌘R | 重新加载界面 |
| 在浏览器中打开 ⌘⇧O | 用默认浏览器打开同一地址（Web 端照常可用） |
| 重启本地服务 ⌘⇧R | 停掉自己拉起的后端再重启 |
| 打开管理后台 ⌘⇧A | 启动并打开后台窗口 |

## 构建 / 安装

```bash
cd 桌面端
./构建.command              # 只构建到 桌面端/PatternFlow.app
./安装到应用程序.command      # 构建 + 安装到 /Applications + 注册到 Spotlight/Launchpad
```

以后改了桌面端代码，重新跑一次 `./安装到应用程序.command` 即可更新安装版。

- 需要 macOS Command Line Tools 的 `swiftc`。本机 Swift 6.3 与 MacOSX27 SDK 不匹配，
  脚本会自动选 `MacOSX26.5.sdk`；没有的话回退默认 SDK。
- 图标源文件：`Resources/icon_source.jpg`（粉色五瓣花）。`Tools/make_icon.py` 负责裁圆角、生成
  `Resources/AppIcon.icns` 和网页品牌小图；换图后重跑脚本再构建即可。
- 产物是 ad-hoc 签名。要分发给别人，需要用 Developer ID 签名并公证（另做）。

## 截图自检

构建后可以不开窗口直接截一张图（用于改版验收）：

```bash
./PatternFlow.app/Contents/MacOS/PatternFlow --snapshot=/tmp/patternflow.png
```

它会自动起后端、加载页面、截图、退出。

## 已知边界

- 桌面端与 Web 端共用同一套后端、同一份数据（`数据/`），不要同时跑两份服务抢端口。
- 管理后台页面目前还是旧视觉（这次只重做了前台对话界面）；下一步可以统一皮肤。
- 机器上另一个项目（Figma-CYQ）占用了 8788/8789，桌面端会自动改用 8790；
  Web 版管理后台若要固定 8788，需要先停掉那个项目或改它的端口。
