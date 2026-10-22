import AppKit
import WebKit

// PatternFlow 桌面端
// 一个原生 WKWebView 壳：启动时确保本地 Python 服务在跑，然后把现有 Web 界面装进来。
// Web 端完全不动 —— 同一套页面、同一套后端，桌面端只是多了原生窗口、菜单和进程守护。

let APP_NAME = "PatternFlow 花型智造"
let CLIENT_PORT = 8787
let ADMIN_PORT = 8788

// MARK: - 本地服务守护

final class Backend {
    static let shared = Backend()

    let repoRoot: URL?
    private(set) var clientOwned = false
    private(set) var adminOwned = false
    private var clientProcess: Process?
    private var adminProcess: Process?
    private var logHandles: [FileHandle] = []

    private init() {
        repoRoot = Backend.locateRepoRoot()
    }

    static func locateRepoRoot() -> URL? {
        var dir = URL(fileURLWithPath: Bundle.main.bundlePath)
        for _ in 0..<6 {
            if FileManager.default.fileExists(atPath: dir.appendingPathComponent("后端/server.py").path) {
                return dir
            }
            dir.deleteLastPathComponent()
        }
        let cwd = URL(fileURLWithPath: FileManager.default.currentDirectoryPath)
        if FileManager.default.fileExists(atPath: cwd.appendingPathComponent("后端/server.py").path) {
            return cwd
        }
        return nil
    }

    private func pythonPath() -> String? {
        let env = ProcessInfo.processInfo.environment
        var candidates: [String] = []
        if let p = env["PATTERNFLOW_PYTHON"], !p.isEmpty { candidates.append(p) }
        candidates.append(contentsOf: [
            "/opt/homebrew/bin/python3.13",
            "/opt/homebrew/bin/python3",
            "/usr/local/bin/python3",
            "/usr/bin/python3",
        ])
        for p in candidates where FileManager.default.isExecutableFile(atPath: p) {
            return p
        }
        return nil
    }

    private func logFile(_ name: String) -> FileHandle? {
        let dir = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Logs/PatternFlow", isDirectory: true)
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        let url = dir.appendingPathComponent(name)
        if !FileManager.default.fileExists(atPath: url.path) {
            FileManager.default.createFile(atPath: url.path, contents: nil)
        }
        guard let h = try? FileHandle(forWritingTo: url) else { return nil }
        h.seekToEndOfFile()
        logHandles.append(h)
        return h
    }

    private func spawn(script: String, dir: URL, port: Int, logName: String) -> Process? {
        guard let python = pythonPath() else { return nil }
        let p = Process()
        p.executableURL = URL(fileURLWithPath: python)
        p.arguments = [script, "--port", "\(port)"]
        p.currentDirectoryURL = dir
        var env = ProcessInfo.processInfo.environment
        env["PYTHONUNBUFFERED"] = "1"
        env["LANG"] = "zh_CN.UTF-8"
        env["LC_ALL"] = "zh_CN.UTF-8"
        p.environment = env
        if let h = logFile(logName) {
            p.standardOutput = h
            p.standardError = h
        }
        do { try p.run(); return p } catch { return nil }
    }

    /// 同步探测端口上的服务是不是**我们自己的**。
    /// 只认 200 + JSON 里带 "ok" —— 不能像以前那样 <500 就算，
    /// 否则别的应用占着 8788 返回 404 也会被当成"后台已启动"。
    func isServing(port: Int) -> Bool {
        guard let url = URL(string: "http://127.0.0.1:\(port)/api/health") else { return false }
        var request = URLRequest(url: url)
        request.timeoutInterval = 1.5
        request.setValue("PatternFlowDesktop/0.1", forHTTPHeaderField: "User-Agent")
        let sem = DispatchSemaphore(value: 0)
        var ok = false
        URLSession.shared.dataTask(with: request) { data, resp, _ in
            if let http = resp as? HTTPURLResponse, http.statusCode == 200,
               let data = data, let text = String(data: data, encoding: .utf8),
               text.contains("\"ok\"") {
                ok = true
            }
            sem.signal()
        }.resume()
        _ = sem.wait(timeout: .now() + 2.2)
        return ok
    }

    func ensureClient(done: @escaping (Bool) -> Void) {
        if isServing(port: CLIENT_PORT) { done(true); return }
        guard let root = repoRoot, let python = pythonPath() else { done(false); return }
        _ = python
        let dir = root.appendingPathComponent("后端", isDirectory: true)
        clientProcess = spawn(script: "server.py", dir: dir, port: CLIENT_PORT, logName: "backend-8787.log")
        clientOwned = clientProcess != nil
        waitUntilServing(port: CLIENT_PORT, deadline: Date().addingTimeInterval(30),
                         process: clientProcess, done: done)
    }

    private(set) var adminPort = ADMIN_PORT

    /// 后台端口可能被别的应用占用（这台机器 8788 就被另一个项目占了），
    /// 所以从 8788 开始顺延，谁先起得来就用谁。
    func ensureAdmin(done: @escaping (Bool) -> Void) {
        let candidates = [ADMIN_PORT, ADMIN_PORT + 1, ADMIN_PORT + 2, ADMIN_PORT + 3]
        func attempt(_ index: Int) {
            if index >= candidates.count { done(false); return }
            let port = candidates[index]
            if isServing(port: port) { adminPort = port; done(true); return }
            guard let root = repoRoot else { done(false); return }
            let dir = root.appendingPathComponent("后台", isDirectory: true)
            let proc = spawn(script: "admin_server.py", dir: dir, port: port,
                             logName: "admin-\(port).log")
            adminProcess = proc
            adminOwned = proc != nil
            waitUntilServing(port: port, deadline: Date().addingTimeInterval(10),
                             process: proc) { [weak self] ok in
                if ok { self?.adminPort = port; done(true) }
                else { attempt(index + 1) }
            }
        }
        attempt(0)
    }

    private func waitUntilServing(port: Int, deadline: Date, process: Process?,
                                  done: @escaping (Bool) -> Void) {
        DispatchQueue.global().async { [weak self] in
            while Date() < deadline {
                if self?.isServing(port: port) == true {
                    DispatchQueue.main.async { done(true) }
                    return
                }
                if let p = process, !p.isRunning {
                    // 进程已经退出：端口被占或脚本报错。再给日志一点落地时间。
                    Thread.sleep(forTimeInterval: 0.5)
                    let ok = self?.isServing(port: port) == true
                    DispatchQueue.main.async { done(ok) }
                    return
                }
                Thread.sleep(forTimeInterval: 0.3)
            }
            DispatchQueue.main.async { done(false) }
        }
    }

    func stopOwned() {
        if clientOwned { clientProcess?.terminate() }
        if adminOwned { adminProcess?.terminate() }
        clientOwned = false
        adminOwned = false
    }
}

// MARK: - 加载中 / 出错遮罩

final class LoadingView: NSView {
    private let spinner = NSProgressIndicator()
    private let label = NSTextField(labelWithString: "正在启动本地服务…")
    private let sub = NSTextField(labelWithString: "第一次启动需要几秒，之后会快很多")
    private let retry = NSButton(title: "重试", target: nil, action: nil)

    override init(frame: NSRect) {
        super.init(frame: frame)
        wantsLayer = true
        layer?.backgroundColor = NSColor(calibratedRed: 0.957, green: 0.961, blue: 0.941, alpha: 1).cgColor
        spinner.style = .spinning
        spinner.controlSize = .regular
        spinner.startAnimation(nil)
        label.font = NSFont(name: "Songti SC", size: 17) ?? NSFont.systemFont(ofSize: 17, weight: .medium)
        label.textColor = NSColor(calibratedRed: 0.09, green: 0.13, blue: 0.12, alpha: 1)
        label.alignment = .center
        sub.font = NSFont.systemFont(ofSize: 12)
        sub.textColor = .secondaryLabelColor
        sub.alignment = .center
        retry.bezelStyle = .rounded
        retry.isHidden = true
        retry.target = self
        retry.action = #selector(retryTapped)
        for v in [spinner, label, sub, retry] {
            v.translatesAutoresizingMaskIntoConstraints = false
            addSubview(v)
        }
        NSLayoutConstraint.activate([
            spinner.centerXAnchor.constraint(equalTo: centerXAnchor),
            spinner.bottomAnchor.constraint(equalTo: label.topAnchor, constant: -16),
            label.centerXAnchor.constraint(equalTo: centerXAnchor),
            label.centerYAnchor.constraint(equalTo: centerYAnchor),
            sub.centerXAnchor.constraint(equalTo: centerXAnchor),
            sub.topAnchor.constraint(equalTo: label.bottomAnchor, constant: 8),
            retry.centerXAnchor.constraint(equalTo: centerXAnchor),
            retry.topAnchor.constraint(equalTo: sub.bottomAnchor, constant: 18),
        ])
    }
    required init?(coder: NSCoder) { fatalError() }

    var onRetry: (() -> Void)?

    func fail(_ message: String) {
        spinner.stopAnimation(nil)
        spinner.isHidden = true
        label.stringValue = "本地服务没起来"
        sub.stringValue = message
        retry.isHidden = false
    }

    @objc private func retryTapped() { onRetry?() }
}

// MARK: - 主窗口

final class MainWindowController: NSWindowController, WKNavigationDelegate, WKUIDelegate {
    private let webView: WKWebView
    private let loadingView: LoadingView
    private var adminWindow: NSWindowController?
    private var snapshotPath: String?

    init(snapshotPath: String? = nil) {
        self.snapshotPath = snapshotPath
        let config = WKWebViewConfiguration()
        config.websiteDataStore = .default()
        config.preferences.setValue(true, forKey: "developerExtrasEnabled")
        webView = WKWebView(frame: .zero, configuration: config)
        loadingView = LoadingView(frame: .zero)

        let window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 1320, height: 860),
            styleMask: [.titled, .closable, .miniaturizable, .resizable],
            backing: .buffered, defer: false)
        window.title = APP_NAME
        window.minSize = NSSize(width: 980, height: 640)
        window.center()
        window.setFrameAutosaveName("PatternFlowMainWindow")
        window.backgroundColor = NSColor(calibratedRed: 0.957, green: 0.961, blue: 0.941, alpha: 1)
        super.init(window: window)

        let container = NSView(frame: window.contentView?.bounds ?? .zero)
        container.autoresizingMask = [.width, .height]
        webView.frame = container.bounds
        webView.autoresizingMask = [.width, .height]
        loadingView.frame = container.bounds
        loadingView.autoresizingMask = [.width, .height]
        container.addSubview(webView)
        container.addSubview(loadingView)
        window.contentView = container

        webView.navigationDelegate = self
        webView.uiDelegate = self
        loadingView.onRetry = { [weak self] in self?.boot() }
        boot()
    }
    required init?(coder: NSCoder) { fatalError() }

    private func boot() {
        loadingView.isHidden = false
        loadingView.fail("")   // 复位
        Backend.shared.ensureClient { [weak self] ok in
            guard let self = self else { return }
            guard ok else {
                self.loadingView.fail("可能是 8787 被别的应用占用，或 后端 不完整。日志见 ~/Library/Logs/PatternFlow/backend-8787.log")
                return
            }
            self.loadHome()
        }
    }

    private func loadHome() {
        guard let url = URL(string: "http://127.0.0.1:\(CLIENT_PORT)/") else { return }
        webView.load(URLRequest(url: url))
    }

    // MARK: 菜单动作
    @objc func reloadPage() {
        if webView.url == nil { loadHome() } else { webView.reload() }
    }

    @objc func openInBrowser() {
        if let url = webView.url { NSWorkspace.shared.open(url) }
    }

    @objc func restartBackend() {
        Backend.shared.stopOwned()
        loadingView.isHidden = false
        loadingView.fail("正在重启本地服务…")
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.8) { [weak self] in self?.boot() }
    }

    @objc func openAdmin() {
        Backend.shared.ensureAdmin { [weak self] ok in
            guard let self = self else { return }
            guard ok, let url = URL(string: "http://127.0.0.1:\(Backend.shared.adminPort)/") else {
                let alert = NSAlert()
                alert.messageText = "管理后台没起来"
                alert.informativeText = "查看 ~/Library/Logs/PatternFlow/admin-8788.log"
                alert.runModal()
                return
            }
            if self.adminWindow == nil {
                let w = NSWindow(
                    contentRect: NSRect(x: 0, y: 0, width: 1240, height: 820),
                    styleMask: [.titled, .closable, .miniaturizable, .resizable],
                    backing: .buffered, defer: false)
                w.title = "PatternFlow 管理后台"
                w.center()
                let cfg = WKWebViewConfiguration()
                cfg.websiteDataStore = .default()
                let wv = WKWebView(frame: w.contentView?.bounds ?? .zero, configuration: cfg)
                wv.autoresizingMask = [.width, .height]
                wv.uiDelegate = self
                w.contentView = wv
                wv.load(URLRequest(url: url))
                self.adminWindow = NSWindowController(window: w)
            }
            self.adminWindow?.showWindow(nil)
            self.adminWindow?.window?.makeKeyAndOrderFront(nil)
        }
    }

    // MARK: WKNavigationDelegate
    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        loadingView.isHidden = true
        if let path = snapshotPath {
            snapshotPath = nil
            DispatchQueue.main.asyncAfter(deadline: .now() + 1.2) { [weak self] in
                self?.takeSnapshot(to: path)
            }
        }
    }

    func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) {
        loadingView.fail("页面没加载出来：\(error.localizedDescription)")
    }

    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) {
        loadingView.fail("页面没加载出来：\(error.localizedDescription)")
    }

    // MARK: WKUIDelegate
    func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration,
                 for navigationAction: WKNavigationAction,
                 windowFeatures: WKWindowFeatures) -> WKWebView? {
        if let url = navigationAction.request.url { NSWorkspace.shared.open(url) }
        return nil
    }

    func webView(_ webView: WKWebView, runOpenPanelWith parameters: WKOpenPanelParameters,
                 initiatedByFrame frame: WKFrameInfo,
                 completionHandler: @escaping ([URL]?) -> Void) {
        let panel = NSOpenPanel()
        panel.allowsMultipleSelection = parameters.allowsMultipleSelection
        panel.canChooseDirectories = false
        panel.canChooseFiles = true
        panel.allowedContentTypes = [.image]
        panel.begin { response in
            completionHandler(response == .OK ? panel.urls : nil)
        }
    }

    func webView(_ webView: WKWebView, runJavaScriptAlertPanelWithMessage message: String,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping () -> Void) {
        let alert = NSAlert()
        alert.messageText = message
        alert.runModal()
        completionHandler()
    }

    // MARK: 截图（用于开发验收）
    private func takeSnapshot(to path: String) {
        let config = WKSnapshotConfiguration()
        config.rect = webView.bounds
        webView.takeSnapshot(with: config) { image, error in
            guard let image = image,
                  let tiff = image.tiffRepresentation,
                  let rep = NSBitmapImageRep(data: tiff),
                  let png = rep.representation(using: .png, properties: [:]) else {
                fputs("snapshot failed: \(error?.localizedDescription ?? "unknown")\n", stderr)
                NSApp.terminate(nil)
                return
            }
            do {
                try png.write(to: URL(fileURLWithPath: path))
                print("snapshot: \(path)")
            } catch {
                fputs("snapshot write failed: \(error)\n", stderr)
            }
            NSApp.terminate(nil)
        }
    }
}

// MARK: - AppDelegate

final class AppDelegate: NSObject, NSApplicationDelegate {
    private var mainWindow: MainWindowController?
    private let snapshotPath: String?

    override init() {
        var path: String? = nil
        for arg in CommandLine.arguments {
            if arg.hasPrefix("--snapshot=") {
                path = String(arg.dropFirst("--snapshot=".count))
            }
        }
        snapshotPath = path
        super.init()
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        if snapshotPath != nil {
            NSApp.setActivationPolicy(.accessory)
        }
        buildMenu()
        mainWindow = MainWindowController(snapshotPath: snapshotPath)
        mainWindow?.showWindow(nil)
        mainWindow?.window?.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        if snapshotPath != nil {
            // 兜底：20 秒内没截到图就退出，避免卡住
            DispatchQueue.main.asyncAfter(deadline: .now() + 20) { NSApp.terminate(nil) }
        }
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        return false
    }

    func applicationWillTerminate(_ notification: Notification) {
        Backend.shared.stopOwned()
    }

    private func buildMenu() {
        let main = NSMenu()

        // 应用菜单
        let appItem = NSMenuItem()
        main.addItem(appItem)
        let appMenu = NSMenu()
        appMenu.addItem(withTitle: "关于 \(APP_NAME)", action: #selector(showAbout), keyEquivalent: "")
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "隐藏 \(APP_NAME)", action: #selector(NSApplication.hide(_:)), keyEquivalent: "h")
        let hideOthers = appMenu.addItem(withTitle: "隐藏其他", action: #selector(NSApplication.hideOtherApplications(_:)), keyEquivalent: "h")
        hideOthers.keyEquivalentModifierMask = [.command, .option]
        appMenu.addItem(withTitle: "全部显示", action: #selector(NSApplication.unhideAllApplications(_:)), keyEquivalent: "")
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "退出 \(APP_NAME)", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        appItem.submenu = appMenu

        // 编辑菜单（WKWebView 里的输入框依赖它才有 ⌘C/⌘V）
        let editItem = NSMenuItem()
        main.addItem(editItem)
        let edit = NSMenu(title: "编辑")
        edit.addItem(withTitle: "撤销", action: Selector(("undo:")), keyEquivalent: "z")
        edit.addItem(withTitle: "重做", action: Selector(("redo:")), keyEquivalent: "Z")
        edit.addItem(.separator())
        edit.addItem(withTitle: "剪切", action: Selector(("cut:")), keyEquivalent: "x")
        edit.addItem(withTitle: "拷贝", action: Selector(("copy:")), keyEquivalent: "c")
        edit.addItem(withTitle: "粘贴", action: Selector(("paste:")), keyEquivalent: "v")
        edit.addItem(withTitle: "全选", action: Selector(("selectAll:")), keyEquivalent: "a")
        editItem.submenu = edit

        // 导航菜单
        let navItem = NSMenuItem()
        main.addItem(navItem)
        let nav = NSMenu(title: "导航")
        let reload = nav.addItem(withTitle: "刷新页面", action: #selector(reloadPage), keyEquivalent: "r")
        reload.target = self
        let browser = nav.addItem(withTitle: "在浏览器中打开", action: #selector(openInBrowser), keyEquivalent: "O")
        browser.keyEquivalentModifierMask = [.command, .shift]
        browser.target = self
        let restart = nav.addItem(withTitle: "重启本地服务", action: #selector(restartBackend), keyEquivalent: "R")
        restart.keyEquivalentModifierMask = [.command, .shift]
        restart.target = self
        nav.addItem(.separator())
        let admin = nav.addItem(withTitle: "打开管理后台", action: #selector(openAdmin), keyEquivalent: "A")
        admin.keyEquivalentModifierMask = [.command, .shift]
        admin.target = self
        navItem.submenu = nav

        // 窗口菜单
        let winItem = NSMenuItem()
        main.addItem(winItem)
        let win = NSMenu(title: "窗口")
        win.addItem(withTitle: "最小化", action: #selector(NSWindow.performMiniaturize(_:)), keyEquivalent: "m")
        win.addItem(withTitle: "缩放", action: #selector(NSWindow.performZoom(_:)), keyEquivalent: "")
        winItem.submenu = win
        NSApp.windowsMenu = win

        NSApp.mainMenu = main
    }

    @objc private func reloadPage() { mainWindow?.reloadPage() }
    @objc private func openInBrowser() { mainWindow?.openInBrowser() }
    @objc private func restartBackend() { mainWindow?.restartBackend() }
    @objc private func openAdmin() { mainWindow?.openAdmin() }

    @objc private func showAbout() {
        let alert = NSAlert()
        alert.messageText = APP_NAME
        alert.informativeText = "桌面端 0.1 · 本地服务 127.0.0.1:8787\nWeb 端仍然保留：在浏览器打开同一地址即可。"
        alert.runModal()
    }
}

// MARK: - 入口

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.regular)
app.run()
