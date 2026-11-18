import Cocoa
import Foundation

struct ServerHealth {
    let outputDirectoryURL: URL
}

private struct ServerHealthPayload: Decodable {
    let ok: Bool
    let output_dir: String
}

func decodeServerHealth(_ data: Data) -> ServerHealth? {
    guard let payload = try? JSONDecoder().decode(ServerHealthPayload.self, from: data),
          payload.ok, (payload.output_dir as NSString).isAbsolutePath else { return nil }
    return ServerHealth(outputDirectoryURL: URL(fileURLWithPath: payload.output_dir, isDirectory: true).standardizedFileURL)
}

private final class HealthResponseBox: @unchecked Sendable {
    private let lock = NSLock()
    private var value: ServerHealth?
    func store(_ health: ServerHealth?) {
        lock.lock()
        defer { lock.unlock() }
        value = health
    }
    func read() -> ServerHealth? {
        lock.lock()
        defer { lock.unlock() }
        return value
    }
}

@discardableResult
func stopServerProcess(_ proc: Process, gracePeriod: TimeInterval = 5.0, killWait: TimeInterval = 1.0) -> Bool {
    guard proc.isRunning else { return true }
    proc.terminate()
    let gracefulDeadline = Date().addingTimeInterval(gracePeriod)
    while proc.isRunning && Date() < gracefulDeadline { Thread.sleep(forTimeInterval: 0.02) }
    if proc.isRunning {
        kill(proc.processIdentifier, SIGKILL)
        let killDeadline = Date().addingTimeInterval(killWait)
        while proc.isRunning && Date() < killDeadline { Thread.sleep(forTimeInterval: 0.02) }
    }
    return !proc.isRunning
}

class AppDelegate: NSObject, NSApplicationDelegate {
    var statusItem: NSStatusItem!
    var serverProcess: Process?
    var boundPort: Int?
    var outputDirectoryURL: URL?
    var portFile: String?
    var isQuitting = false
    var openedInitial = false

    func applicationDidFinishLaunching(_ notification: Notification) {
        setupStatusBar()
        startServer()
    }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        openWorkspace()
        return true
    }

    func applicationWillTerminate(_ notification: Notification) {
        isQuitting = true
        if let proc = serverProcess { stopServerProcess(proc) }
        if let path = portFile { try? FileManager.default.removeItem(atPath: path) }
    }

    func setupStatusBar() {
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        statusItem.button?.title = "📖 KPW6精排"
        rebuildMenu(status: "正在启动本地引擎...")
    }

    func rebuildMenu(status: String) {
        let menu = NSMenu()
        let openItem = NSMenuItem(title: "🌐 打开 Kindle 精排工作台", action: #selector(openWorkspace), keyEquivalent: "o")
        openItem.target = self
        openItem.isEnabled = boundPort != nil
        menu.addItem(openItem)

        let dirItem = NSMenuItem(title: "📂 打开导出文件夹", action: #selector(openOutputDir), keyEquivalent: "d")
        dirItem.target = self
        dirItem.isEnabled = outputDirectoryURL != nil
        menu.addItem(dirItem)

        menu.addItem(NSMenuItem.separator())
        let statItem = NSMenuItem(title: status, action: nil, keyEquivalent: "")
        statItem.isEnabled = false
        menu.addItem(statItem)
        menu.addItem(NSMenuItem.separator())

        let quitItem = NSMenuItem(title: "退出 Kindle 精排大师", action: #selector(quitApp), keyEquivalent: "q")
        quitItem.target = self
        menu.addItem(quitItem)
        statusItem.menu = menu
    }

    func startServer() {
        guard let resDir = Bundle.main.resourcePath else { return }
        let serverBin = (resDir as NSString).appendingPathComponent("server/KPW6StudioServer")
        let fm = FileManager.default
        if !fm.isExecutableFile(atPath: serverBin) {
            showFatalAlert("未找到内置引擎文件：\(serverBin)")
            return
        }

        let portFile = (NSTemporaryDirectory() as NSString).appendingPathComponent("kpw6_port_\(getpid()).txt")
        self.portFile = portFile
        try? fm.removeItem(atPath: portFile)

        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: serverBin)
        proc.arguments = ["--no-browser", "--port-file", portFile]
        var env = ProcessInfo.processInfo.environment
        env["KPW6_NO_BROWSER"] = "1"
        proc.environment = env
        proc.terminationHandler = { [weak self] _ in
            DispatchQueue.main.async {
                guard let self = self, !self.isQuitting else { return }
                self.rebuildMenu(status: "后台服务已停止")
            }
        }

        do {
            try proc.run()
            self.serverProcess = proc
        } catch {
            showFatalAlert("启动精排引擎失败：\(error.localizedDescription)")
            return
        }

        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            guard let self = self else { return }
            let deadline = Date().addingTimeInterval(25.0)
            while Date() < deadline {
                if self.isQuitting { return }
                if !proc.isRunning {
                    DispatchQueue.main.async {
                        guard !self.isQuitting else { return }
                        self.showFatalAlert("精排引擎启动异常退出，请检查 ~/Documents/KPW6_PDF_Studio 写入权限。")
                    }
                    return
                }
                if let raw = try? String(contentsOfFile: portFile, encoding: .utf8),
                   let p = Int(raw.trimmingCharacters(in: .whitespacesAndNewlines)),
                   p > 0 {
                    if let health = self.checkServerHealth(port: p) {
                        DispatchQueue.main.async {
                            guard !self.isQuitting else { return }
                            self.boundPort = p
                            self.outputDirectoryURL = health.outputDirectoryURL
                            self.rebuildMenu(status: "运行中 · http://127.0.0.1:\(p)")
                            if !self.openedInitial {
                                self.openedInitial = true
                                self.openWorkspace()
                            }
                        }
                        try? fm.removeItem(atPath: portFile)
                        return
                    }
                }
                Thread.sleep(forTimeInterval: 0.2)
            }
            DispatchQueue.main.async {
                guard !self.isQuitting else { return }
                self.showFatalAlert("本地服务响应超时（25秒），请退出后重试。")
            }
        }
    }

    func checkServerHealth(port: Int) -> ServerHealth? {
        guard let url = URL(string: "http://127.0.0.1:\(port)/api/health") else { return nil }
        let sem = DispatchSemaphore(value: 0)
        let result = HealthResponseBox()
        let cfg = URLSessionConfiguration.ephemeral
        cfg.connectionProxyDictionary = [:]
        let session = URLSession(configuration: cfg)
        defer { session.invalidateAndCancel() }
        let req = URLRequest(url: url, timeoutInterval: 1.0)
        session.dataTask(with: req) { data, resp, _ in
            if let data = data, let http = resp as? HTTPURLResponse, http.statusCode == 200 {
                result.store(decodeServerHealth(data))
            }
            sem.signal()
        }.resume()
        guard sem.wait(timeout: .now() + 1.2) == .success else { return nil }
        return result.read()
    }

    @objc func openWorkspace() {
        if let port = boundPort, let url = URL(string: "http://127.0.0.1:\(port)") {
            NSWorkspace.shared.open(url)
        }
    }

    @objc func openOutputDir() {
        guard let directory = outputDirectoryURL else { return }
        NSWorkspace.shared.open(directory)
    }

    @objc func quitApp() {
        NSApplication.shared.terminate(nil)
    }

    func showFatalAlert(_ message: String) {
        let alert = NSAlert()
        alert.messageText = "KPW6 PDF Studio"
        alert.informativeText = message
        alert.alertStyle = .critical
        alert.addButton(withTitle: "退出")
        alert.runModal()
        NSApplication.shared.terminate(nil)
    }
}

let app = NSApplication.shared
app.setActivationPolicy(.regular)
let delegate = AppDelegate()
app.delegate = delegate
app.run()
