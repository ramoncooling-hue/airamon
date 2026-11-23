import AppKit
import WebKit
import Darwin

final class DesktopApp: NSObject, NSApplicationDelegate, NSWindowDelegate,
    WKNavigationDelegate, WKUIDelegate, WKDownloadDelegate {
    private var window: NSWindow!
    private var web: WKWebView!
    private var backend: Process?
    private var logHandle: FileHandle?
    private var startupTimer: Timer?
    private var lockFD: Int32 = -1
    private var baseURL: URL?
    private var stopping = false
    private var maintaining = false
    private var failing = false
    private var maintenanceProcess: Process?
    private var loaded = false
    private var startupDeadline = Date()
    private let token = UUID().uuidString + UUID().uuidString
    // Proves the health probe reached the backend this process just launched.
    private let readyNonce = UUID().uuidString
    private let dataRoot: URL = {
        if let override = ProcessInfo.processInfo.environment["CYBERPAL_DATA_DIR"] {
            return URL(fileURLWithPath: override, isDirectory: true)
        }
        return FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Application Support/MinDock", isDirectory: true)
    }()
    private var sessionFile: URL { dataRoot.appendingPathComponent("app/desktop-session.json") }

    func applicationDidFinishLaunching(_ notification: Notification) {
        do {
            try FileManager.default.createDirectory(at: dataRoot.appendingPathComponent("app/logs"),
                withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
            try FileManager.default.setAttributes([.posixPermissions: 0o700], ofItemAtPath: dataRoot.path)
            lockFD = open(dataRoot.appendingPathComponent("app/desktop.lock").path, O_CREAT | O_RDWR, 0o600)
            guard lockFD >= 0, flock(lockFD, LOCK_EX | LOCK_NB) == 0 else {
                NSRunningApplication.runningApplications(withBundleIdentifier: "local.mindock.desktop")
                    .first(where: { $0.processIdentifier != ProcessInfo.processInfo.processIdentifier })?
                    .activate(options: [.activateAllWindows])
                NSApp.terminate(nil)
                return
            }
            setupMenu()
            let config = WKWebViewConfiguration()
            web = WKWebView(frame: .zero, configuration: config)
            web.navigationDelegate = self
            web.uiDelegate = self
            web.allowsBackForwardNavigationGestures = false
            window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1380, height: 900),
                styleMask: [.titled, .closable, .miniaturizable, .resizable], backing: .buffered, defer: false)
            window.title = "MinDock 思泊"
            window.minSize = NSSize(width: 1080, height: 680)
            window.contentView = web
            window.delegate = self
            window.center()
            window.setFrameAutosaveName("MinDockMainWindow")
            window.makeKeyAndOrderFront(nil)
            NSApp.activate(ignoringOtherApps: true)
            web.loadHTMLString("""
                <html><body style="background:#faf9fd;color:#35244c;font:18px -apple-system;display:grid;place-content:center;height:90vh;text-align:center">
                <h1>MinDock 思泊</h1><p>正在打开你的个人知识空间...</p></body></html>
                """, baseURL: nil)
            try startBackend()
        } catch {
            fail("无法启动 MinDock", error.localizedDescription)
        }
    }

    private func setupMenu() {
        let menu = NSMenu()
        let appMenu = NSMenu()
        appMenu.addItem(withTitle: "关于 MinDock 思泊", action: #selector(about), keyEquivalent: "")
        appMenu.addItem(NSMenuItem.separator())
        appMenu.addItem(withTitle: "退出 MinDock", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        let appItem = NSMenuItem(); appItem.submenu = appMenu; menu.addItem(appItem)
        let editMenu = NSMenu(title: "编辑")
        for (title, action, key) in [("撤销", "undo:", "z"), ("剪切", "cut:", "x"),
                                    ("复制", "copy:", "c"), ("粘贴", "paste:", "v"), ("全选", "selectAll:", "a")] {
            editMenu.addItem(withTitle: title, action: Selector(action), keyEquivalent: key)
        }
        let editItem = NSMenuItem(title: "编辑", action: nil, keyEquivalent: "")
        editItem.submenu = editMenu; menu.addItem(editItem)
        let viewMenu = NSMenu(title: "显示")
        viewMenu.addItem(withTitle: "刷新页面", action: #selector(reload), keyEquivalent: "r")
        viewMenu.addItem(withTitle: "返回上一页", action: #selector(goBack), keyEquivalent: "[")
        viewMenu.addItem(withTitle: "放大", action: #selector(zoomIn), keyEquivalent: "+")
        viewMenu.addItem(withTitle: "缩小", action: #selector(zoomOut), keyEquivalent: "-")
        viewMenu.addItem(withTitle: "实际大小", action: #selector(resetZoom), keyEquivalent: "0")
        let viewItem = NSMenuItem(title: "显示", action: nil, keyEquivalent: "")
        viewItem.submenu = viewMenu; menu.addItem(viewItem)
        let dataMenu = NSMenu(title: "资料")
        dataMenu.addItem(withTitle: "打开数据文件夹", action: #selector(openData), keyEquivalent: "d")
        dataMenu.addItem(withTitle: "打开运行日志", action: #selector(openLogs), keyEquivalent: "")
        dataMenu.addItem(NSMenuItem.separator())
        dataMenu.addItem(withTitle: "备份个人资料...", action: #selector(backupData), keyEquivalent: "")
        dataMenu.addItem(withTitle: "从备份恢复...", action: #selector(restoreData), keyEquivalent: "")
        let dataItem = NSMenuItem(title: "资料", action: nil, keyEquivalent: "")
        dataItem.submenu = dataMenu; menu.addItem(dataItem)
        NSApp.mainMenu = menu
    }

    private func startBackend() throws {
        loaded = false
        baseURL = nil
        guard let resources = Bundle.main.resourceURL else { throw NSError(domain: "MinDock", code: 1) }
        let executable = resources.appendingPathComponent("backend/mindock-backend")
        guard FileManager.default.isExecutableFile(atPath: executable.path) else {
            throw NSError(domain: "MinDock", code: 2, userInfo: [NSLocalizedDescriptionKey: "安装包缺少内置运行环境，请重新安装。"])
        }
        try? FileManager.default.removeItem(at: sessionFile)
        let log = dataRoot.appendingPathComponent("app/logs/desktop.log")
        let logValues = try? log.resourceValues(forKeys: [.fileSizeKey])
        let logSize = logValues?.fileSize ?? 0
        if logSize > 5_000_000 {
            let previous = log.deletingLastPathComponent().appendingPathComponent("desktop-previous.log")
            try? FileManager.default.removeItem(at: previous)
            try? FileManager.default.moveItem(at: log, to: previous)
        }
        if !FileManager.default.fileExists(atPath: log.path) {
            FileManager.default.createFile(atPath: log.path, contents: nil, attributes: [.posixPermissions: 0o600])
        }
        try? logHandle?.close()
        logHandle = try FileHandle(forWritingTo: log)
        logHandle?.seekToEndOfFile()
        var env = ProcessInfo.processInfo.environment
        for key in ["PYTHONHOME", "PYTHONPATH", "PYTHONSTARTUP", "VIRTUAL_ENV"] { env.removeValue(forKey: key) }
        env["CYBERPAL_DATA_DIR"] = dataRoot.path
        env["MINDOCK_SESSION_FILE"] = sessionFile.path
        env["MINDOCK_SESSION_TOKEN"] = token
        env["MINDOCK_READY_NONCE"] = readyNonce
        env["PATH"] = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
        env["MINDOCK_OCR"] = resources.appendingPathComponent("mindock-ocr").path
        env["PYTHONUNBUFFERED"] = "1"
        let process = Process()
        process.executableURL = executable
        process.environment = env
        process.currentDirectoryURL = resources
        process.standardOutput = logHandle
        process.standardError = logHandle
        process.terminationHandler = { [weak self] _ in
            DispatchQueue.main.async {
                guard let self = self, !self.stopping, !self.maintaining else { return }
                self.fail("本地服务已停止", "资料没有被删除。请重新打开应用，或通过「资料 → 打开运行日志」查看原因。")
            }
        }
        try process.run()
        backend = process
        startupDeadline = Date().addingTimeInterval(120)
        startupTimer = Timer.scheduledTimer(withTimeInterval: 0.4, repeats: true) { [weak self] _ in self?.checkReady() }
    }

    private func checkReady() {
        if Date() > startupDeadline {
            startupTimer?.invalidate()
            fail("启动等待超时", "请查看运行日志。首次初始化较慢；重新打开应用会保留已经创建的资料。")
            return
        }
        guard !loaded, let data = try? Data(contentsOf: sessionFile),
            let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Int],
            let port = obj["port"], let url = URL(string: "http://127.0.0.1:\(port)") else { return }
        var request = URLRequest(url: url.appendingPathComponent("api/health"), timeoutInterval: 2)
        request.setValue(token, forHTTPHeaderField: "X-MinDock-Session")
        URLSession.shared.dataTask(with: request) { [weak self] data, response, _ in
            guard (response as? HTTPURLResponse)?.statusCode == 200 else { return }
            DispatchQueue.main.async {
                guard let self = self, !self.loaded else { return }
                let payload = (try? JSONSerialization.jsonObject(with: data ?? Data())) as? [String: Any]
                // A stale or foreign local service on the recovered port must not be loaded.
                guard (payload?["nonce"] as? String) == self.readyNonce else {
                    self.startupTimer?.invalidate()
                    self.fail("本地服务校验失败", "端口上响应的不是本次启动的 MinDock 服务，已停止加载以免泄露资料。请重新打开应用。")
                    return
                }
                self.loaded = true
                self.startupTimer?.invalidate()
                self.baseURL = url
                let cookie = HTTPCookie(properties: [.domain: "127.0.0.1", .path: "/",
                    .name: "mindock_session", .value: self.token, .discard: "TRUE",
                    HTTPCookiePropertyKey("HttpOnly"): "TRUE", .sameSitePolicy: "Strict"])!
                self.web.configuration.websiteDataStore.httpCookieStore.setCookie(cookie) {
                    self.web.load(URLRequest(url: url, cachePolicy: .reloadIgnoringLocalCacheData))
                }
            }
        }.resume()
    }

    private func fail(_ title: String, _ message: String) {
        guard !failing else { return }
        failing = true
        startupTimer?.invalidate()
        backend?.terminationHandler = nil
        let alert = NSAlert()
        alert.messageText = title; alert.informativeText = message
        let canRecover = window != nil && lockFD >= 0
        if canRecover { alert.addButton(withTitle: "从备份恢复") }
        alert.addButton(withTitle: "打开日志"); alert.addButton(withTitle: "退出")
        let response = alert.runModal()
        failing = false
        if canRecover && response == .alertFirstButtonReturn { restoreData(); return }
        if response == (canRecover ? .alertSecondButtonReturn : .alertFirstButtonReturn) { openLogs() }
        NSApp.terminate(nil)
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }
    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        if maintaining {
            let alert = NSAlert()
            alert.messageText = "正在处理备份"
            alert.informativeText = "完成后即可退出，避免中断资料恢复。"
            alert.runModal()
            return .terminateCancel
        }
        return .terminateNow
    }
    func windowShouldClose(_ sender: NSWindow) -> Bool { !maintaining }
    func applicationWillTerminate(_ notification: Notification) {
        stopping = true
        startupTimer?.invalidate()
        if let process = backend, process.isRunning {
            process.terminate()
            let deadline = Date().addingTimeInterval(10)
            while process.isRunning && Date() < deadline { Thread.sleep(forTimeInterval: 0.05) }
            if process.isRunning { kill(process.processIdentifier, SIGKILL) }
        }
        try? logHandle?.close()
        if lockFD >= 0 { flock(lockFD, LOCK_UN); close(lockFD) }
    }

    func webView(_ webView: WKWebView, decidePolicyFor action: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        if action.shouldPerformDownload { decisionHandler(.download); return }
        if let url = action.request.url, ["http", "https"].contains(url.scheme ?? ""),
           !(url.host == "127.0.0.1" && url.port == baseURL?.port) {
            NSWorkspace.shared.open(url); decisionHandler(.cancel); return
        }
        decisionHandler(.allow)
    }

    func webView(_ webView: WKWebView, decidePolicyFor response: WKNavigationResponse,
                 decisionHandler: @escaping (WKNavigationResponsePolicy) -> Void) {
        decisionHandler(response.canShowMIMEType ? .allow : .download)
    }
    func webView(_ webView: WKWebView, navigationAction: WKNavigationAction, didBecome download: WKDownload) { download.delegate = self }
    func webView(_ webView: WKWebView, navigationResponse: WKNavigationResponse, didBecome download: WKDownload) { download.delegate = self }
    func download(_ download: WKDownload, decideDestinationUsing response: URLResponse,
                  suggestedFilename: String, completionHandler: @escaping (URL?) -> Void) {
        let panel = NSSavePanel(); panel.nameFieldStringValue = suggestedFilename
        panel.beginSheetModal(for: window) { result in completionHandler(result == .OK ? panel.url : nil) }
    }
    func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration,
                 for action: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
        if let url = action.request.url {
            if url.host == "127.0.0.1", url.port == baseURL?.port { webView.load(action.request) }
            else if ["http", "https"].contains(url.scheme ?? "") { NSWorkspace.shared.open(url) }
        }
        return nil
    }
    func webView(_ webView: WKWebView, requestMediaCapturePermissionFor origin: WKSecurityOrigin,
                 initiatedByFrame frame: WKFrameInfo, type: WKMediaCaptureType,
                 decisionHandler: @escaping (WKPermissionDecision) -> Void) {
        // baseURL is set only after this launch's backend passes the nonce probe.
        guard webView === web, loaded, !stopping, !maintaining, !failing,
              backend?.isRunning == true, type == .microphone,
              let baseURL = baseURL, baseURL.scheme == "http", baseURL.host == "127.0.0.1",
              let port = baseURL.port, (1...65535).contains(port),
              origin.protocol == baseURL.scheme, origin.host == baseURL.host, origin.port == port,
              frame.securityOrigin.protocol == baseURL.scheme,
              frame.securityOrigin.host == baseURL.host, frame.securityOrigin.port == port,
              let pageURL = webView.url, pageURL.scheme == baseURL.scheme,
              pageURL.host == baseURL.host, pageURL.port == port else {
            decisionHandler(.deny)
            return
        }
        // Keep WebKit/macOS consent; this microphone API does not enable system-audio capture.
        decisionHandler(.prompt)
    }
    func webView(_ webView: WKWebView, runOpenPanelWith parameters: WKOpenPanelParameters,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping ([URL]?) -> Void) {
        let panel = NSOpenPanel()
        panel.canChooseFiles = true; panel.canChooseDirectories = parameters.allowsDirectories
        panel.allowsMultipleSelection = parameters.allowsMultipleSelection
        panel.beginSheetModal(for: window) { result in completionHandler(result == .OK ? panel.urls : nil) }
    }
    func webView(_ webView: WKWebView, runJavaScriptAlertPanelWithMessage message: String,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping () -> Void) {
        let alert = NSAlert(); alert.messageText = message; alert.addButton(withTitle: "好")
        alert.beginSheetModal(for: window) { _ in completionHandler() }
    }
    func webView(_ webView: WKWebView, runJavaScriptConfirmPanelWithMessage message: String,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping (Bool) -> Void) {
        let alert = NSAlert(); alert.messageText = message
        alert.addButton(withTitle: "确认"); alert.addButton(withTitle: "取消")
        alert.beginSheetModal(for: window) { result in completionHandler(result == .alertFirstButtonReturn) }
    }
    func webView(_ webView: WKWebView, runJavaScriptTextInputPanelWithPrompt prompt: String,
                 defaultText: String?, initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping (String?) -> Void) {
        let alert = NSAlert(); alert.messageText = prompt
        let input = NSTextField(frame: NSRect(x: 0, y: 0, width: 340, height: 24)); input.stringValue = defaultText ?? ""
        alert.accessoryView = input; alert.addButton(withTitle: "确认"); alert.addButton(withTitle: "取消")
        alert.beginSheetModal(for: window) { result in completionHandler(result == .alertFirstButtonReturn ? input.stringValue : nil) }
    }

    @objc private func about() {
        NSApp.orderFrontStandardAboutPanel(options: [.applicationName: "MinDock 思泊",
            .applicationVersion: Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? "",
            .credits: NSAttributedString(string: "让思考有所停泊，让成长自有航向\n资料保存在本机，不需要注册账号。")])
    }
    @objc private func openData() { NSWorkspace.shared.open(dataRoot) }
    @objc private func openLogs() { NSWorkspace.shared.open(dataRoot.appendingPathComponent("app/logs")) }
    @objc private func backupData() {
        guard !maintaining, loaded else { return }
        let panel = NSSavePanel()
        let formatter = DateFormatter(); formatter.dateFormat = "yyyyMMdd-HHmmss"
        panel.nameFieldStringValue = "MinDock-backup-\(formatter.string(from: Date())).zip"
        panel.beginSheetModal(for: window) { [weak self] result in
            guard let self = self, result == .OK, let url = panel.url else { return }
            if FileManager.default.fileExists(atPath: url.path) {
                let alert = NSAlert(); alert.messageText = "请选择新的备份文件名"
                alert.informativeText = "已有备份不会被覆盖。"; alert.runModal(); return
            }
            self.confirmMaintenance("backup", archive: url)
        }
    }
    @objc private func restoreData() {
        guard !maintaining else { return }
        let panel = NSOpenPanel()
        panel.canChooseDirectories = false; panel.allowsMultipleSelection = false
        panel.beginSheetModal(for: window) { [weak self] result in
            guard let self = self, result == .OK, let url = panel.url else { return }
            self.inspectArchive(url)
        }
    }
    private func inspectArchive(_ archive: URL) {
        guard !maintaining, let resources = Bundle.main.resourceURL else { return }
        maintaining = true
        let process = Process()
        process.executableURL = resources.appendingPathComponent("backend/mindock-backend")
        process.arguments = ["--maintenance", "inspect", "--archive", archive.path]
        var env = ProcessInfo.processInfo.environment
        for key in ["PYTHONHOME", "PYTHONPATH", "PYTHONSTARTUP", "VIRTUAL_ENV"] { env.removeValue(forKey: key) }
        env["CYBERPAL_DATA_DIR"] = dataRoot.path
        process.environment = env
        let pipe = Pipe(); process.standardOutput = pipe; process.standardError = pipe
        maintenanceProcess = process
        window.title = "MinDock 思泊 · 正在校验备份"
        DispatchQueue.global(qos: .userInitiated).async {
            var summary: [String: Any]?
            var failure: String?
            do {
                try process.run()
                let output = pipe.fileHandleForReading.readDataToEndOfFile()
                process.waitUntilExit()
                if process.terminationStatus != 0 {
                    failure = String(data: output, encoding: .utf8)?.suffix(1000).description ?? "备份校验失败"
                } else {
                    summary = try JSONSerialization.jsonObject(with: output) as? [String: Any]
                    if summary == nil { failure = "无法读取备份摘要" }
                }
            } catch { failure = error.localizedDescription }
            DispatchQueue.main.async {
                self.maintaining = false; self.maintenanceProcess = nil
                self.window.title = "MinDock 思泊"
                if let failure = failure {
                    let alert = NSAlert(); alert.messageText = "无法恢复此备份"
                    alert.informativeText = failure; alert.runModal(); return
                }
                guard let summary = summary else { return }
                let created = summary["created_at"] as? String ?? "未知"
                let version = summary["app_version"] as? String ?? "未知"
                let count = summary["file_count"] as? Int ?? 0
                let size = summary["total_bytes"] as? Int64 ?? 0
                let detail = "备份日期：\(created)\n应用版本：\(version)\n文件：\(count) 个，\(ByteCountFormatter.string(fromByteCount: size, countStyle: .file))"
                self.confirmMaintenance("restore", archive: archive, summary: detail,
                    archiveHash: summary["sha256"] as? String)
            }
        }
    }
    private func confirmMaintenance(_ operation: String, archive: URL, summary: String = "", archiveHash: String? = nil) {
        let alert = NSAlert()
        alert.messageText = operation == "backup" ? "备份个人资料" : "用备份恢复个人资料？"
        alert.informativeText = operation == "backup"
            ? "将暂时停止识别和对话。备份包含材料、图谱、日历和对话，不含模型密钥。完成后自动重新打开。\n\(archive.path)"
            : "\(summary)\n\n将暂时停止服务并替换当前资料，替换前会保留当前数据副本。备份不含模型密钥，恢复后需重新填写。\n\(archive.lastPathComponent)"
        alert.addButton(withTitle: operation == "backup" ? "开始备份" : "确认恢复")
        alert.addButton(withTitle: "取消")
        alert.beginSheetModal(for: window) { [weak self] result in
            if result == .alertFirstButtonReturn { self?.runMaintenance(operation, archive: archive, archiveHash: archiveHash) }
        }
    }
    private func runMaintenance(_ operation: String, archive: URL, archiveHash: String? = nil) {
        guard !maintaining, let resources = Bundle.main.resourceURL else { return }
        maintaining = true
        startupTimer?.invalidate()
        backend?.terminationHandler = nil
        let previousBackend = backend
        web.loadHTMLString("""
            <html><body style="background:#faf9fd;color:#35244c;font:18px -apple-system;display:grid;place-content:center;height:90vh;text-align:center">
            <h1>MinDock 思泊</h1><p>正在\(operation == "backup" ? "备份" : "恢复")个人资料，请保持应用打开...</p></body></html>
            """, baseURL: nil)
        let process = Process()
        process.executableURL = resources.appendingPathComponent("backend/mindock-backend")
        process.arguments = ["--maintenance", operation, "--archive", archive.path]
        if let archiveHash = archiveHash { process.arguments! += ["--archive-sha256", archiveHash] }
        var env = ProcessInfo.processInfo.environment
        for key in ["PYTHONHOME", "PYTHONPATH", "PYTHONSTARTUP", "VIRTUAL_ENV"] { env.removeValue(forKey: key) }
        env["CYBERPAL_DATA_DIR"] = dataRoot.path
        env["MINDOCK_MAINTENANCE_OWNER_PID"] = String(ProcessInfo.processInfo.processIdentifier)
        process.environment = env; process.currentDirectoryURL = resources
        let output = Pipe(); process.standardOutput = output; process.standardError = output
        maintenanceProcess = process
        DispatchQueue.global(qos: .userInitiated).async {
            var errorMessage: String?
            var recoveryDirectory: String?
            var recoveryWarning = ""
            do {
                if let previous = previousBackend, previous.isRunning {
                    previous.terminate()
                    let deadline = Date().addingTimeInterval(15)
                    while previous.isRunning && Date() < deadline { Thread.sleep(forTimeInterval: 0.05) }
                    if previous.isRunning {
                        throw NSError(domain: "MinDock", code: 3,
                            userInfo: [NSLocalizedDescriptionKey: "识别任务尚未停止。本次未操作资料，请稍后重试。"])
                    }
                }
                try process.run()
                let data = output.fileHandleForReading.readDataToEndOfFile()
                process.waitUntilExit()
                if process.terminationStatus != 0 {
                    errorMessage = String(data: data, encoding: .utf8)?.suffix(1200).description ?? "操作失败，请查看日志。"
                } else if let response = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
                    recoveryDirectory = response["recovery_dir"] as? String
                    if response["previous_database_corrupt"] as? Bool == true {
                        recoveryWarning = "\n原数据库已损坏，原始文件已保留在上述副本中。"
                    }
                }
            } catch { errorMessage = error.localizedDescription }
            DispatchQueue.main.async {
                self.maintenanceProcess = nil
                self.maintaining = false
                // Maintenance preserves app/ and both lock files; retain the shell lock throughout.
                let alert = NSAlert()
                alert.messageText = errorMessage == nil ? (operation == "backup" ? "备份完成" : "恢复完成") : "操作未完成"
                alert.informativeText = errorMessage ?? (operation == "backup" ? archive.path : "正在重新打开。请在设置中检查模型配置。\n原资料副本：\(recoveryDirectory ?? "资料目录 app/recovery")\(recoveryWarning)")
                alert.addButton(withTitle: "继续")
                alert.runModal()
                if previousBackend?.isRunning == true {
                    self.fail("服务未能停止", "本次未操作资料。请退出后重新打开应用。"); return
                }
                do { try self.startBackend() }
                catch { self.fail("无法重新打开 MinDock", error.localizedDescription) }
            }
        }
    }
    @objc private func reload() { web?.reloadFromOrigin() }
    @objc private func goBack() { web?.goBack() }
    @objc private func zoomIn() { web.pageZoom = min(1.5, web.pageZoom + 0.1) }
    @objc private func zoomOut() { web.pageZoom = max(0.7, web.pageZoom - 0.1) }
    @objc private func resetZoom() { web.pageZoom = 1 }
}

let application = NSApplication.shared
let delegate = DesktopApp()
application.delegate = delegate
application.setActivationPolicy(.regular)
application.run()
