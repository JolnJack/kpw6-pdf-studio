import Cocoa
import Foundation

class AppDelegate: NSObject, NSApplicationDelegate {
    var statusItem: NSStatusItem!
    var serverProcess: Process?
    var boundPort: Int = 8765
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
        if let proc = serverProcess, proc.isRunning {
            proc.terminate()
            DispatchQueue.global().asyncAfter(deadline: .now() + 1.0) {
                if proc.isRunning { kill(proc.processIdentifier, SIGKILL) }
            }
        }
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
        menu.addItem(openItem)

        let dirItem = NSMenuItem(title: "📂 打开导出文件夹 (Documents/KPW6_PDF_Studio)", action: #selector(openOutputDir), keyEquivalent: "d")
        dirItem.target = self
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
        try? fm.removeItem(atPath: portFile)

        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: serverBin)
        proc.arguments = ["--no-browser", "--port-file", portFile]
        var env = ProcessInfo.processInfo.environment
        env["KPW6_NO_BROWSER"] = "1"
        proc.environment = env
        proc.terminationHandler = { [weak self] _ in
            DispatchQueue.main.async {
                self?.rebuildMenu(status: "后台服务已停止")
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
                if !proc.isRunning {
                    DispatchQueue.main.async {
                        self.showFatalAlert("精排引擎启动异常退出，请检查 ~/Documents/KPW6_PDF_Studio 写入权限。")
                    }
                    return
                }
                if let raw = try? String(contentsOfFile: portFile, encoding: .utf8),
                   let p = Int(raw.trimmingCharacters(in: .whitespacesAndNewlines)),
                   p > 0 {
                    if self.checkServerHealth(port: p) {
                        DispatchQueue.main.async {
                            self.boundPort = p
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
                self.showFatalAlert("本地服务响应超时（25秒），请退出后重试。")
            }
        }
    }

    func checkServerHealth(port: Int) -> Bool {
        guard let url = URL(string: "http://127.0.0.1:\(port)/api/health") else { return false }
        let sem = DispatchSemaphore(value: 0)
        var ok = false
        let cfg = URLSessionConfiguration.ephemeral
        cfg.connectionProxyDictionary = [:]
        let session = URLSession(configuration: cfg)
        let req = URLRequest(url: url, timeoutInterval: 1.0)
        session.dataTask(with: req) { _, resp, _ in
            if let http = resp as? HTTPURLResponse, http.statusCode == 200 {
                ok = true
            }
            sem.signal()
        }.resume()
        _ = sem.wait(timeout: .now() + 1.2)
        return ok
    }

    @objc func openWorkspace() {
        if let url = URL(string: "http://127.0.0.1:\(boundPort)") {
            NSWorkspace.shared.open(url)
        }
    }

    @objc func openOutputDir() {
        let outDir = (NSHomeDirectory() as NSString).appendingPathComponent("Documents/KPW6_PDF_Studio/outputs")
        try? FileManager.default.createDirectory(atPath: outDir, withIntermediateDirectories: true)
        NSWorkspace.shared.open(URL(fileURLWithPath: outDir))
    }

    @objc func quitApp() {
        NSApplication.shared.terminate(nil)
    }

    func showFatalAlert(_ message: String) {
        let alert = NSAlert()
        alert.messageText = "Kindle 精排大师"
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
