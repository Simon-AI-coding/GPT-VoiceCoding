import Foundation
import Observation

/// How an ask ended when it did not bring back an answer.
///
/// Three of the four failure kinds this surface must keep apart. The fourth —
/// there is no engine process at all — is process parenthood and lives in
/// ``EngineHealth``, because "the engine refused", "nothing answered" and "there
/// is no engine" are different sentences. So is "the engine answered in an
/// unsupported protocol", and a merged rendering would hide the one fact that
/// tells the user which side needs updating.
public enum ActionFailure: Equatable, Sendable {
    /// **(a)** Bridge Core answered, and answered no. Its own words, verbatim.
    case refused(Refusal)
    /// **(b)** Nothing answered. Raised here, never sent by the engine.
    case unreachable(String)
    /// **(c)** The engine answered in a protocol this shell cannot interpret.
    case protocolMismatch(String)
}

/// What the last `status` read found.
public enum StatusReading: Equatable, Sendable {
    case notYetRead
    case read(EngineStatus)
    case failed(ActionFailure)
}

private enum CountedState {
    case waiting, finished

    init?(_ state: String) {
        switch state {
        case "decision", "permission": self = .waiting
        case "finished": self = .finished
        default: return nil
        }
    }
}

/// The single counting rule shared by Home and the Duty Card.
public struct SessionCounts: Equatable, Sendable {
    public var sessions = 0
    public var waiting = 0
    public var finished = 0
    public var childProcesses = 0

    init(sessions: Int = 0, waiting: Int = 0, finished: Int = 0, childProcesses: Int = 0) {
        self.sessions = sessions
        self.waiting = waiting
        self.finished = finished
        self.childProcesses = childProcesses
    }

    init(rows: [JSONValue]) {
        for row in rows where row["lifecycle"]?.string == "live" {
            if row["child"]?["kind"]?.string == "child" || row["headless_run"]?.bool == true {
                childProcesses += 1
                continue
            }
            sessions += 1
            switch CountedState(row["brief_state"]?.string ?? "") {
            case .waiting: waiting += 1
            case .finished: finished += 1
            case nil: break
            }
        }
    }
}

/// The status facts this desktop displays. Counts have one owner and one rule.
public struct EngineStatus: Equatable, Sendable {
    public let engineVersion: String?
    public let callAgent: CallAgentReading?
    public let switches: [SwitchReading]
    public let callID: String?
    public let dialAttempt: String?
    public let counts: SessionCounts
    public var callIsUp: Bool { callID != nil }

    public init(document: [String: JSONValue]) {
        engineVersion = document["engine_version"]?.string
        callAgent = document["call_agent"]?.object.map(CallAgentReading.init)
        switches = SwitchReading.canonicalOrder.compactMap { name in
            document["switches"]?[name]?.bool.map { SwitchReading(name: name, on: $0) }
        }
        callID = document["call_id"]?.string
        dialAttempt = document["dial_attempt"]?.string
        counts = SessionCounts(rows: document["sessions"]?.array ?? [])
    }
}

/// The wire address that identifies one Session and links a Child Process to its parent.
public struct SessionAddress: CustomStringConvertible, Equatable, Hashable, Sendable {
    public var agent: String
    public var sessionID: String?
    public var pid: Int?

    public var description: String {
        let process = pid.map { ":\($0)" } ?? ""
        return "\(agent):\(sessionID ?? "")\(process)"
    }

    init(_ value: JSONValue) {
        agent = value["agent"]?.string ?? ""
        sessionID = value["session_id"]?.string
        pid = value["pid"]?.number.map(Int.init)
    }

    var payload: JSONValue {
        var fields: [String: JSONValue] = ["agent": .string(agent)]
        if let sessionID { fields["session_id"] = .string(sessionID) }
        if let pid { fields["pid"] = .number(Double(pid)) }
        return .object(fields)
    }
}

/// Core's roster order and words, with no shell-side naming or classification.
public struct BriefRow: Equatable, Identifiable, Sendable {
    public let target: SessionAddress
    public let name: String?
    public let state: String
    public let stateWord: String
    public let newest: String
    public let messageAt: Date?
    public var id: SessionAddress { target }
    fileprivate var isCounted: Bool { CountedState(state) != nil }

    init(_ value: JSONValue) {
        target = SessionAddress(value["target"] ?? .null)
        name = value["name"]?.string
        state = value["state"]?.string ?? ""
        stateWord = value["state_word"]?.string ?? ""
        newest = value["newest"]?.string ?? ""
        messageAt = messageDate(value["message_at"]?.string)
    }
}

public struct DesktopReminder: Equatable, Sendable {
    public let id: String
    public let row: BriefRow

    init?(_ value: JSONValue?) {
        guard let id = value?["id"]?.string, !id.isEmpty,
            let row = value?["row"], row["target"] != nil
        else { return nil }
        self.id = id
        self.row = BriefRow(row)
    }
}

public struct SessionBriefReading: Equatable, Sendable {
    public let name: String?
    public let stateWord: String
    public let newest: String
    public let messageAt: Date?
    public let prompt: String?
    public let options: [String]

    init(_ value: JSONValue) {
        name = value["name"]?.string
        stateWord = value["state_word"]?.string ?? ""
        newest = value["newest"]?["text"]?.string ?? ""
        messageAt = messageDate(value["newest"]?["occurred_at"]?.string)
        prompt = value["decision"]?["prompt"]?.string
        options = (value["decision"]?["options"]?.array ?? []).enumerated().map { index, option in
            let description = option["description"]?.string ?? ""
            return "\(index + 1). \(option["text"]?.string ?? "")"
                + (description.isEmpty ? "" : " — \(description)")
        }
    }
}

public struct CallAgentReading: Equatable, Sendable {
    public let model: String
    public let effort: String?
    public let contextPercent: Int?
    public let total: [String: Int]?
    public let last: [String: Int]?

    init(_ document: [String: JSONValue]) {
        model = document["model"]?.string ?? ""
        effort = document["effort"]?.string
        contextPercent = document["context_percent"]?.number.map(Int.init)
        total = document["total"]?.object.map { $0.compactMapValues { $0.number.map(Int.init) } }
        last = document["last"]?.object.map { $0.compactMapValues { $0.number.map(Int.init) } }
    }
}

public struct SwitchReading: Equatable, Sendable, Identifiable {
    /// Every switch this shell has a row for: the engine's wire key, and the
    /// Language's own words for it. One table, so a switch cannot be half-known
    /// — ordered here and untitled, or titled here and never rendered.
    ///
    /// The order is the Language's: Duty is the master, the next two are
    /// effective only while it is on, and Auto Hang-up stands beside Duty rather
    /// than under it — the Silence Ceiling is the call's own limit, so it holds
    /// with Duty off.
    private static let known: [(name: String, title: String)] = [
        ("duty", "Duty Switch"),
        ("voice", "Voice Switch"),
        ("message", "Message Switch"),
        ("auto_hangup", "Auto Hang-up Switch"),
    ]

    /// The rendering order, and the panel maps `status` over it rather than over
    /// the reply's own keys: a switch the engine grows and this shell has no row
    /// for is ignored, not guessed at.
    public static let canonicalOrder = known.map(\.name)

    public var name: String
    public var on: Bool
    public var id: String { name }

    /// The Language's own words, so the dropdown never invents a second name for
    /// a switch that already has one.
    public var title: String {
        Self.known.first { $0.name == name }?.title ?? name
    }
}

/// What the Live Toggle last reported. `state` is rendered as the engine sent it.
public struct LiveReading: Equatable, Sendable {
    public var state: String
    public var callID: String?
}

public enum CallPhase: String, CaseIterable, Sendable {
    case ready, calling, onCall, ending, couldNotConnect
    public var resolving: Bool { self == .calling || self == .ending }
}

public struct CallAgentModel: Equatable, Sendable {
    public let model: String
    public let efforts: [String]
    init(_ value: JSONValue) {
        model = value["model"]?.string ?? ""
        efforts = value["efforts"]?.array?.compactMap(\.string) ?? []
    }
}

/// Control-plane readings and desktop presentation, behind the existing dialer.
/// Core owns calls, words and roster order. This module owns the shared counts,
/// transient Call Phases and Home's input-safety hold.
@MainActor
@Observable
public final class ControlPanel {
    public private(set) var models: [CallAgentModel] = []
    public private(set) var modelsFailure: ActionFailure?
    public private(set) var sessionBrief: SessionBriefReading?
    private var sessionTarget: SessionAddress?
    public private(set) var sessionFailure: ActionFailure?
    public private(set) var nextCallStartsFresh = false
    public private(set) var phase: CallPhase = .ready
    public private(set) var elapsed: Int?
    private var dialingAttempt: String?
    private var cancellingAttempt: String?
    private var cancelledAttempt: String?
    private var phaseStarted: TimeInterval = 0
    private let now: () -> TimeInterval
    public private(set) var roster: [BriefRow] = []
    public private(set) var desktopReminder: DesktopReminder?
    public private(set) var rosterRevision = 0
    public private(set) var rosterReachable = false
    private var heldRoster: [BriefRow]?
    public var displayedRoster: [BriefRow] { heldRoster ?? roster }
    public var firstCountedRow: BriefRow? { roster.first { $0.isCounted } }
    public private(set) var status: EngineStatus?
    private var statusFailure: ActionFailure?
    public var reading: StatusReading {
        if let statusFailure { return .failed(statusFailure) }
        if let status { return .read(status) }
        return .notYetRead
    }
    public var engineReachable: Bool { status != nil && statusFailure == nil }
    /// How the last thing the user asked for ended, or nil when it worked. Held
    /// apart from ``reading`` so the re-read that follows an action cannot erase
    /// the refusal that action earned.
    public private(set) var lastFailure: ActionFailure?
    public private(set) var live: LiveReading?
    /// The last `verify` answer, when the user asked for one. Never on a timer:
    /// it asks every seam about itself, which is a question, not a heartbeat.
    public private(set) var seams: [SeamReading]?
    public private(set) var busy = false

    public var counts: SessionCounts {
        if case .read(let status) = reading { return status.counts }
        return SessionCounts()
    }

    public var dutyOn: Bool {
        status?.switches.first { $0.name == "duty" }?.on ?? false
    }

    /// Whether the system owns a call, or nil when nothing has said.
    ///
    /// `status` wins whenever there is one, because every toggle is followed by a
    /// re-read and because *another* surface may have ended the call since. A
    /// panel that kept preferring its own last Live Toggle answer would be
    /// holding call state — the thing that once let two toggles open two calls —
    /// and it would go stale silently, which is worse than being wrong loudly.
    ///
    /// The Live Toggle's own reply is used when status is unavailable. The five
    /// shell-timed phases describe the request, not a second call owner.
    public var callIsUp: Bool? {
        if case .read(let status) = reading { return status.callIsUp }
        if let live { return live.state == "up" }
        return nil
    }

    private let client: ControlPlaneDialing

    public init(
        client: ControlPlaneDialing,
        now: @escaping () -> TimeInterval = { ProcessInfo.processInfo.systemUptime }
    ) {
        self.client = client
        self.now = now
    }

    /// Read `status`. Cheap, and never gated by any switch (ADR 0002) — the
    /// window works from a machine with Duty off, which is the whole point.
    public func refresh() async {
        let outcome = await ask(Request(action: .status), { EngineStatus(document: $0) })
        guard !Task.isCancelled else { return }
        switch outcome {
        case .answered(let status):
            self.status = status
            statusFailure = nil
            reconcilePhase()
        case .failed(let failure): statusFailure = failure
        }
    }

    public func refreshRoster() async {
        let outcome = await ask(Request(action: .brief)) { $0["roster"] ?? .null }
        guard !Task.isCancelled else { return }
        if let answer = outcome.answer {
            roster = (answer["rows"]?.array ?? []).map(BriefRow.init)
            desktopReminder = DesktopReminder(answer["desktop_reminder"])
            rosterReachable = true
            rosterRevision += 1
        } else {
            rosterReachable = false
            desktopReminder = nil
        }
    }

    public func setPointerInRoster(_ inside: Bool) {
        if inside {
            if heldRoster == nil { heldRoster = roster }
        } else {
            heldRoster = nil
        }
    }

    public func refreshSession(_ target: SessionAddress) async {
        sessionTarget = target
        let outcome = await ask(Request(action: .brief, payload: ["target": target.payload])) {
            SessionBriefReading($0["session"] ?? .null)
        }
        guard !Task.isCancelled, sessionTarget == target else { return }
        sessionBrief = outcome.answer
        sessionFailure = outcome.failure
    }

    public func clearSession() {
        sessionTarget = nil
        sessionBrief = nil
        sessionFailure = nil
    }

    /// A caller asks for confirmation before entering here during a call.
    public func newCallAgent() async {
        if phase == .onCall {
            await toggleLive()
            if phase == .ready, lastFailure == nil { await toggleLive() }
        } else {
            busy = true
            defer { busy = false }
            let outcome = await ask(Request(action: .forgetCallAgent)) { $0 }
            await refresh()
            nextCallStartsFresh = outcome.failure == nil
            lastFailure = outcome.failure
        }
    }

    /// Flip one switch, then re-read, so what is shown is what Bridge Core holds
    /// rather than what this surface just asked for.
    public func flip(_ name: String, on: Bool) async {
        busy = true
        defer { busy = false }
        let outcome = await ask(
            Request(action: .switch, payload: ["name": .string(name), "on": .bool(on)])
        ) { $0 }
        await refresh()
        // Recorded after the re-read, never before: a switch Bridge Core refused
        // is news the user is owed even when the next read succeeds.
        lastFailure = outcome.failure
    }

    /// The Live Toggle — one action, the same one `bridgectl live` calls.
    ///
    /// Bridge Core owns the policy: it ends the call the system owns, or starts
    /// one if none is up. This surface times the request and its displayed phase;
    /// it never chooses a different engine action for dial and hang-up.
    public func toggleLive() async {
        guard !phase.resolving else { return }
        busy = true
        defer { busy = false }
        let ending = callIsUp == true
        nextCallStartsFresh = false
        setPhase(ending ? .ending : .calling)
        let attempt = ending ? nil : UUID().uuidString
        cancelledAttempt = nil
        dialingAttempt = attempt
        defer { dialingAttempt = nil }
        let payload = attempt.map { ["attempt_id": JSONValue.string($0)] }
        let outcome = await ask(Request(action: .live, payload: payload)) {
            LiveReading(state: $0["state"]?.string ?? "", callID: $0["call_id"]?.string)
        }
        // Only what the engine sent. A failed toggle leaves the last reading
        // alone rather than guessing which way the call went.
        if let answer = outcome.answer { live = answer }
        if let answer = outcome.answer, answer.state == "up" {
            setPhase(.onCall)
        } else {
            setPhase(
                ending
                    || (attempt != nil
                        && (cancellingAttempt == attempt || cancelledAttempt == attempt)
                        && outcome.answer?.state == "down")
                    ? .ready : .couldNotConnect)
        }
        await refresh()
        lastFailure = outcome.failure
    }

    public func cancelDial() async {
        guard phase == .calling, cancellingAttempt == nil,
            let attempt = dialingAttempt ?? status?.dialAttempt
        else { return }
        cancellingAttempt = attempt
        defer { cancellingAttempt = nil }
        let outcome = await ask(Request(action: .live, payload: ["cancel_dial": .string(attempt)]))
        {
            $0["cancelled"]?.bool == true
        }
        if outcome.answer == true { cancelledAttempt = attempt }
        await refresh()
        if outcome.answer == true { setPhase(callIsUp == true ? .onCall : .ready) }
        lastFailure = outcome.failure
    }

    /// What the engine actually loaded, per ADR 0003. Asked because a person
    /// asked.
    public func verify() async {
        busy = true
        defer { busy = false }
        let outcome = await ask(Request(action: .verify)) { document in
            (document["seams"]?.array ?? []).map(SeamReading.init)
        }
        if let answer = outcome.answer { seams = answer }
        lastFailure = outcome.failure
    }

    public func bindTelegram(_ payload: [String: JSONValue] = [:]) async -> TelegramBindingReading?
    {
        let outcome = await ask(
            Request(action: .bindTelegram, payload: payload), TelegramBindingReading.init)
        guard !Task.isCancelled else { return nil }
        lastFailure = outcome.failure
        return outcome.answer
    }

    public func refreshModels() async {
        guard engineReachable else { return }
        let outcome = await ask(Request(action: .models)) {
            ($0["models"]?.array ?? []).map(CallAgentModel.init)
        }
        guard !Task.isCancelled else { return }
        models = outcome.answer ?? []
        modelsFailure = outcome.failure
    }

    /// Presentation time only. Core still owns whether a call exists.
    public func tick() {
        reconcilePhase()
        elapsed = phase == .calling || phase == .onCall ? Int(now() - phaseStarted) : nil
    }

    private func reconcilePhase() {
        guard !busy else { return }
        if status?.dialAttempt != nil {
            setPhase(.calling)
            return
        }
        if phase == .couldNotConnect, now() - phaseStarted < 6 { return }
        setPhase(callIsUp == true ? .onCall : .ready)
    }

    private func setPhase(_ phase: CallPhase) {
        if self.phase != phase {
            self.phase = phase
            phaseStarted = now()
        }
        elapsed = phase == .calling || phase == .onCall ? Int(now() - phaseStarted) : nil
    }

    /// One request, and the three ways it can end without an answer.
    private func ask<T>(
        _ request: Request, _ read: ([String: JSONValue]) -> T
    ) async -> Outcome<T> {
        do {
            let reply = try await client.ask(request)
            if let refusal = reply.refusal {
                // Its own words, carried, not rephrased.
                return .failed(.refused(refusal))
            }
            return .answered(read(reply.data))
        } catch let failure as ControlPlaneFailure {
            switch failure {
            case .protocolMismatch:
                return .failed(.protocolMismatch(failure.detail))
            case .engineUnreachable, .unreadable:
                return .failed(.unreachable(failure.detail))
            }
        } catch {
            return .failed(.unreachable("\(error)"))
        }
    }
}

/// An answer, or the reason there is none.
enum Outcome<T> {
    case answered(T)
    case failed(ActionFailure)

    var answer: T? {
        if case .answered(let value) = self { return value }
        return nil
    }

    var failure: ActionFailure? {
        if case .failed(let failure) = self { return failure }
        return nil
    }
}

/// One row of `verify`: what configuration named, what the adapter says about
/// itself, and which of the three outcomes that is.
public struct SeamReading: Equatable, Sendable, Identifiable {
    public var seam: String
    public var outcome: String
    public var configured: String
    public var loaded: String
    public var detail: String
    public var id: String { seam }

    public init(_ value: JSONValue) {
        seam = value["seam"]?.string ?? ""
        outcome = value["outcome"]?.string ?? ""
        configured = value["configured"]?.string ?? ""
        loaded = value["loaded"]?.string ?? ""
        detail = value["detail"]?.string ?? ""
    }
}

private func messageDate(_ value: String?) -> Date? {
    guard let value else { return nil }
    let format = ISO8601DateFormatter()
    format.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
    if let date = format.date(from: value) { return date }
    format.formatOptions = [.withInternetDateTime]
    return format.date(from: value)
}
