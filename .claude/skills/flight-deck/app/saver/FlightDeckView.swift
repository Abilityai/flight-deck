import Network
import ScreenSaver

/// Flight Deck screensaver: the light one. A clock, the Claude agents, the subscriptions' limits
/// and one machine line, drawn natively and redrawn once every 5 seconds from the feeder's
/// /lite.json. No web view, no animation loop. The full monitor (graphs, sessions view) is the
/// `flight-deck` window, not this.
///
/// The host (legacyScreenSaver) is sandboxed and its App Transport Security settings are not
/// ours to change, so the JSON is fetched over a plain TCP connection (Network.framework).
@objc(FlightDeckView)
final class FlightDeckView: ScreenSaverView {
    private static let port: UInt16 = 47600
    private static let every: TimeInterval = 5
    private var timer: Timer?
    private var state: [String: Any]?
    private var lastOK = Date.distantPast

    override init?(frame: NSRect, isPreview: Bool) {
        super.init(frame: frame, isPreview: isPreview)
        setUp()
    }

    required init?(coder: NSCoder) {
        super.init(coder: coder)
        setUp()
    }

    private func setUp() {
        animationTimeInterval = Self.every
        // Since macOS 14 the host does not reliably call stopAnimation() or free the view when
        // the saver is dismissed, so old instances pile up and keep running. Quit the host
        // process when it announces the stop; it is relaunched on the next activation.
        DistributedNotificationCenter.default().addObserver(
            self, selector: #selector(hostWillStop), name: NSNotification.Name("com.apple.screensaver.willstop"), object: nil)
        let t = Timer(timeInterval: Self.every, repeats: true) { [weak self] _ in self?.refresh() }
        t.tolerance = 1
        RunLoop.main.add(t, forMode: .common)
        timer = t
        refresh()
    }

    @objc private func hostWillStop() {
        // The System Settings thumbnail lives in the same kind of host; only quit full-screen runs.
        if bounds.width > 500 { exit(0) }
    }

    private func refresh() {
        Self.get("/lite.json") { [weak self] body in
            guard let self else { return }
            if let body, let obj = try? JSONSerialization.jsonObject(with: body) as? [String: Any] {
                self.state = obj
                self.lastOK = Date()
            }
            self.needsDisplay = true
        }
    }

    // MARK: drawing

    override var isFlipped: Bool { true }
    override var isOpaque: Bool { true }
    override func animateOneFrame() {}
    override var hasConfigureSheet: Bool { false }
    override var configureSheet: NSWindow? { nil }

    private static let bg = NSColor(srgbRed: 0.016, green: 0.024, blue: 0.043, alpha: 1)
    private static let ink = rgb(0xdbe6f6), dim = rgb(0x6b7a93), faint = rgb(0x2a3446)
    private static let amber = rgb(0xffb04a), green = rgb(0x5df2a1), red = rgb(0xff5d73)
    private static let acc = [0x58e1ff, 0xa78bfa, 0x5df2a1, 0xff8ad0, 0x7aa2ff, 0xe8e08a, 0xff9f7a].map(rgb)

    private static func rgb(_ v: Int) -> NSColor {
        NSColor(srgbRed: CGFloat((v >> 16) & 255) / 255, green: CGFloat((v >> 8) & 255) / 255,
                blue: CGFloat(v & 255) / 255, alpha: 1)
    }

    private enum Align { case left, right }

    private func text(_ s: String, _ x: CGFloat, _ y: CGFloat, _ w: CGFloat, _ font: NSFont, _ color: NSColor,
                      _ align: Align = .left, kern: CGFloat = 0) {
        let p = NSMutableParagraphStyle()
        p.lineBreakMode = .byTruncatingTail
        p.alignment = align == .left ? .left : .right
        let a: [NSAttributedString.Key: Any] = [.font: font, .foregroundColor: color, .paragraphStyle: p, .kern: kern]
        (s as NSString).draw(with: NSRect(x: x, y: y, width: w, height: font.pointSize * 1.5),
                             options: [.usesLineFragmentOrigin, .truncatesLastVisibleLine], attributes: a)
    }

    private func bar(_ x: CGFloat, _ y: CGFloat, _ w: CGFloat, _ h: CGFloat, _ pct: Double?) {
        Self.faint.withAlphaComponent(0.6).setFill()
        NSRect(x: x, y: y, width: w, height: h).fill()
        guard let pct else { return }
        (pct >= 90 ? Self.red : pct >= 70 ? Self.amber : Self.green).setFill()
        NSRect(x: x, y: y, width: w * CGFloat(min(100, max(0, pct)) / 100), height: h).fill()
    }

    private func ago(_ ts: Double) -> String {
        let s = max(0, Date().timeIntervalSince1970 - ts)
        if s < 90 { return "now" }
        if s < 5400 { return "\(Int(s / 60))m" }
        if s < 129_600 { return "\(Int(s / 3600))h" }
        return "\(Int(s / 86400))d"
    }

    override func draw(_ rect: NSRect) {
        Self.bg.setFill()
        bounds.fill()

        let u = min(bounds.width / 1920, bounds.height / 1080)
        let online = Date().timeIntervalSince(lastOK) < 20
        let agents = online ? (state?["agents"] as? [[String: Any]] ?? []) : []
        let accounts = online ? (state?["accounts"] as? [[String: Any]] ?? []).filter {
            ($0["live"] as? Int ?? 0) > 0 || $0["five"] is NSNumber || $0["week"] is NSNumber
        } : []
        let shown = Array(agents.prefix(6))

        // layout: one column, measured first so it can sit anywhere on the screen
        let W = 640 * u
        let agentH = 58 * u, subH = 34 * u, headH = 44 * u
        var H = 190 * u + 34 * u                                   // clock + date
        if !shown.isEmpty { H += headH + CGFloat(shown.count) * agentH }
        if !accounts.isEmpty { H += headH + CGFloat(accounts.count) * subH }
        H += 50 * u                                                // machine line

        // The block moves to a new spot every minute, so nothing stays lit in one place.
        let now = Date()
        let minute = UInt64(now.timeIntervalSince1970 / 60)
        func frac(_ salt: UInt64) -> CGFloat {
            var z = (minute &+ salt) &* 0x9E37_79B9_7F4A_7C15
            z = (z ^ (z >> 30)) &* 0xBF58_476D_1CE4_E5B9
            z = (z ^ (z >> 27)) &* 0x94D0_49BB_1331_11EB
            return CGFloat((z ^ (z >> 31)) % 10_000) / 10_000
        }
        let margin = 70 * u
        let x0 = margin + max(0, bounds.width - W - 2 * margin) * frac(1)
        var y = margin + max(0, bounds.height - H - 2 * margin) * frac(2)

        let cal = Calendar.current
        let c = cal.dateComponents([.hour, .minute], from: now)
        let clock = String(format: "%02d:%02d", c.hour ?? 0, c.minute ?? 0)
        text(clock, x0 - 6 * u, y, W, .monospacedDigitSystemFont(ofSize: 150 * u, weight: .ultraLight), Self.ink)
        y += 190 * u
        let df = DateFormatter()
        df.dateFormat = "EEEE d MMMM"
        let small = NSFont.monospacedSystemFont(ofSize: 14 * u, weight: .regular)
        text(df.string(from: now).uppercased(), x0, y, W, small, Self.dim, kern: 2.4 * u)
        y += 34 * u

        if !shown.isEmpty {
            let busy = agents.filter { $0["busy"] as? Bool == true }.count
            y += 18 * u
            text("AGENTS", x0, y, W, small, Self.dim, kern: 2.4 * u)
            let sum = busy > 0 ? "\(busy) working · \(agents.count - busy) idle" : "\(agents.count) idle"
            text(sum, x0, y, W, small, busy > 0 ? Self.amber : Self.dim, .right)
            y += headH - 18 * u
            for a in shown {
                let on = a["busy"] as? Bool == true
                let d = NSBezierPath(ovalIn: NSRect(x: x0 + 2 * u, y: y + 8 * u, width: 9 * u, height: 9 * u))
                if on { Self.amber.setFill(); d.fill() } else { Self.dim.setStroke(); d.lineWidth = u; d.stroke() }
                text(a["project"] as? String ?? "?", x0 + 26 * u, y, W - 110 * u,
                     .systemFont(ofSize: 20 * u, weight: .medium), on ? Self.amber : Self.ink)
                text(ago(a["since"] as? Double ?? 0), x0, y + 4 * u, W, small, on ? Self.amber : Self.dim, .right)
                text(a["text"] as? String ?? "", x0 + 26 * u, y + 27 * u, W - 26 * u,
                     .systemFont(ofSize: 14.5 * u, weight: .regular), Self.dim)
                y += agentH
            }
        }

        if !accounts.isEmpty {
            y += 18 * u
            text("SUBSCRIPTIONS", x0, y, W, small, Self.dim, kern: 2.4 * u)
            text("5H", x0, y, W - 150 * u, small, Self.dim, .right, kern: 1.2 * u)
            text("WEEK", x0, y, W, small, Self.dim, .right, kern: 1.2 * u)
            y += headH - 18 * u
            for a in accounts {
                let ci = a["ci"] as? Int ?? 0
                Self.acc[abs(ci) % Self.acc.count].setFill()
                NSRect(x: x0 + 2 * u, y: y + 5 * u, width: 9 * u, height: 9 * u).fill()
                var name = a["name"] as? String ?? "?"
                if let live = a["live"] as? Int, live > 0 { name += "  · \(live) live" }
                text(name, x0 + 26 * u, y - 2 * u, W - 300 * u, .systemFont(ofSize: 16 * u, weight: .regular), Self.ink)
                for (i, key) in ["five", "week"].enumerated() {
                    let v = (a[key] as? NSNumber)?.doubleValue
                    let bx = x0 + W - 280 * u + CGFloat(i) * 150 * u      // each column is 130 wide
                    bar(bx, y + 7 * u, 74 * u, 5 * u, v)
                    text(v.map { "\(Int($0.rounded()))%" } ?? "–", bx + 80 * u, y, 50 * u, small, v == nil ? Self.dim : Self.ink, .right)
                }
                y += subH
            }
        }

        y += 18 * u
        var bits: [String] = []
        if online, let m = state?["machine"] as? [String: Any] {
            if let v = m["cpu"] as? Double { bits.append("CPU \(Int(v.rounded()))%") }
            if let v = m["mem"] as? Double { bits.append("MEM \(Int(v.rounded()))%") }
            if let b = m["battery"] as? [String: Any], let p = b["pct"] as? Double {
                bits.append((b["plugged"] as? Bool == true ? "AC " : "BATTERY ") + "\(Int(p.rounded()))%")
            }
            if let b = state?["backup"] as? [String: Any], let last = b["last"] as? Double {
                bits.append(b["ok"] as? Bool == false ? "BACKUP FAILED" : "BACKUP \(ago(last).uppercased())")
            }
        } else {
            bits.append("FEEDER OFFLINE")
        }
        text(bits.joined(separator: "   ·   "), x0, y, W, small, Self.dim, kern: 1.2 * u)
    }

    // MARK: data

    /// Minimal HTTP/1.0 GET against the feeder. Calls back on the main queue with the body, or nil.
    private static func get(_ path: String, done: @escaping (Data?) -> Void) {
        let conn = NWConnection(host: "127.0.0.1", port: NWEndpoint.Port(rawValue: port)!, using: .tcp)
        var buf = Data()
        var finished = false
        func finish(_ ok: Bool) {
            if finished { return }
            finished = true
            conn.cancel()
            var body: Data?
            if ok, let r = buf.range(of: Data("\r\n\r\n".utf8)),
               String(data: buf.prefix(15), encoding: .ascii)?.contains(" 200") == true {
                body = buf.subdata(in: r.upperBound..<buf.endIndex)
            }
            DispatchQueue.main.async { done(body) }
        }
        func read() {
            conn.receive(minimumIncompleteLength: 1, maximumLength: 1 << 20) { data, _, complete, error in
                if let data { buf.append(data) }
                if complete || error != nil { finish(error == nil || !buf.isEmpty) } else { read() }
            }
        }
        conn.stateUpdateHandler = { state in
            switch state {
            case .ready:
                conn.send(content: Data("GET \(path) HTTP/1.0\r\nHost: 127.0.0.1\r\n\r\n".utf8),
                          completion: .contentProcessed { _ in })
                read()
            case .failed, .waiting: finish(false)
            default: break
            }
        }
        let q = DispatchQueue.global(qos: .utility)
        conn.start(queue: q)
        q.asyncAfter(deadline: .now() + 4) { finish(false) }
    }
}
