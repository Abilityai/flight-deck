// flight-deck — open the Flight Deck page in its own chromeless window.
//   flight-deck          open it; if it is open but behind, bring it to the front;
//                        if it is already in front, close it (so one hotkey toggles)
//   flight-deck close    close it
// The window is a dedicated Chrome instance (own profile in ~/.cache/flight-deck/chrome): with
// several Chrome instances running, that is the only dependable way to find, focus and close
// exactly this window. The page keeps running while visible and sleeps only when hidden.
import AppKit

func port() -> Int {
    let f = ProcessInfo.processInfo.environment["FLIGHT_DECK_CONFIG"] ?? NSHomeDirectory() + "/.config/flight-deck/config.json"
    if let d = FileManager.default.contents(atPath: f),
       let j = (try? JSONSerialization.jsonObject(with: d)) as? [String: Any], let p = j["port"] as? Int { return p }
    return 47600
}
let url = "http://127.0.0.1:\(port())/"
let profile = NSHomeDirectory() + "/.cache/flight-deck/chrome"
let marker = "--user-data-dir=" + profile

func commandLine(_ pid: pid_t) -> String {
    let p = Process(), out = Pipe()
    p.executableURL = URL(fileURLWithPath: "/bin/ps")
    p.arguments = ["-o", "command=", "-p", String(pid)]
    p.standardOutput = out
    try? p.run(); p.waitUntilExit()
    return String(data: out.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8) ?? ""
}

let mine = NSWorkspace.shared.runningApplications.first {
    $0.bundleIdentifier == "com.google.Chrome" && commandLine($0.processIdentifier).contains(marker)
}
let wantClose = CommandLine.arguments.dropFirst().first == "close"

/// Chrome stays alive after its last window is closed (⌘W, the red button), so "running" does
/// not mean "there is a window". Ask the window server for a real window of that process.
func hasWindow(_ pid: pid_t) -> Bool {
    let all = CGWindowListCopyWindowInfo([.optionAll], kCGNullWindowID) as? [[String: Any]] ?? []
    return all.contains { w in
        guard (w[kCGWindowOwnerPID as String] as? Int).map(pid_t.init) == pid,
              (w[kCGWindowLayer as String] as? Int) == 0,
              let b = w[kCGWindowBounds as String] as? [String: Any],
              let h = b["Height"] as? Double, let wd = b["Width"] as? Double else { return false }
        return h > 200 && wd > 200
    }
}

func openWindow() {
    let p = Process()
    p.executableURL = URL(fileURLWithPath: "/usr/bin/open")
    p.arguments = ["-na", "Google Chrome", "--args", marker, "--no-first-run", "--no-default-browser-check", "--app=" + url]
    try? p.run(); p.waitUntilExit()
}

if let app = mine {
    if wantClose { app.terminate() }
    else if !hasWindow(app.processIdentifier) {          // alive but windowless: give it a window again
        openWindow()
        Thread.sleep(forTimeInterval: 0.8)
        app.activate(options: [.activateAllWindows])
    }
    else if app.isActive { app.terminate() }
    else { app.activate(options: [.activateAllWindows]) }
} else if !wantClose {
    openWindow()
    // Bring the new window to the front once it exists.
    for _ in 0..<30 {
        Thread.sleep(forTimeInterval: 0.2)
        if let app = NSWorkspace.shared.runningApplications.first(where: {
            $0.bundleIdentifier == "com.google.Chrome" && commandLine($0.processIdentifier).contains(marker) }) {
            Thread.sleep(forTimeInterval: 0.6)
            app.activate(options: [.activateAllWindows])
            break
        }
    }
}
