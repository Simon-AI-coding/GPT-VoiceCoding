import Foundation
import Testing

@testable import ShellCore

func desktopReply(_ action: Action, _ data: [String: Any]) -> String {
    let bytes = try! JSONSerialization.data(withJSONObject: [
        "ok": true, "action": action.rawValue, "protocol": controlPlaneProtocolVersion,
        "data": data,
    ])
    return String(decoding: bytes, as: UTF8.self)
}

func desktopRow(
    _ name: String, state: String = "decision", word: String = "waiting for your decision"
) -> [String: Any] {
    [
        "target": ["agent": "codex", "session_id": name], "name": name, "agent": "codex",
        "state": state, "state_word": word, "newest": "The newest message for \(name)",
    ]
}

@MainActor
@Suite struct DesktopPanelTests {
    @Test func desktopFactsArriveFromTheFakeEngineOnARealSocket() async throws {
        let engine = try FakeEngineSocket(
            behaviour: .script([
                "status": desktopReply(
                    .status,
                    [
                        "engine_version": "1.2.3", "switches": ["duty": true],
                        "call_agent": [
                            "model": "chosen-model", "effort": "high", "context_percent": 37,
                            "total": ["input": 120, "output": 30, "reasoning": 20, "cached": 10],
                            "last": ["input": 60, "output": 15, "reasoning": 10, "cached": 5],
                        ],
                        "sessions": [
                            ["lifecycle": "live", "brief_state": "decision"],
                            ["lifecycle": "live", "brief_state": "finished"],
                            [
                                "lifecycle": "live", "brief_state": "decision",
                                "child": ["kind": "child"],
                            ],
                            ["lifecycle": "live", "brief_state": "finished", "headless_run": true],
                            ["lifecycle": "ended", "brief_state": "finished"],
                        ],
                    ]),
                "brief": desktopReply(
                    .brief,
                    [
                        "roster": [
                            "rows": [
                                desktopRow("Latest", state: "finished", word: "finished"),
                                desktopRow("Earlier"),
                            ]
                        ]
                    ]),
            ]))
        defer { engine.stop() }
        let panel = ControlPanel(client: UnixSocketControlPlane(path: engine.path))
        await panel.refresh()
        await panel.refreshRoster()
        #expect(panel.dutyOn)
        #expect(
            panel.counts == SessionCounts(sessions: 2, waiting: 1, finished: 1, childProcesses: 2))
        #expect(panel.displayedRoster.map(\.name) == ["Latest", "Earlier"])
        #expect(panel.firstCountedRow?.name == "Latest")
        #expect(panel.status?.engineVersion == "1.2.3")
        #expect(panel.status?.callAgent?.model == "chosen-model")
        #expect(panel.status?.callAgent?.contextPercent == 37)
        #expect(
            panel.status?.callAgent?.total == [
                "input": 120, "output": 30, "reasoning": 20, "cached": 10,
            ])
        #expect(
            panel.status?.callAgent?.last == [
                "input": 60, "output": 15, "reasoning": 10, "cached": 5,
            ])
    }

    @Test func aSessionBriefKeepsTheWholeNewestAndNumbersTheDecision() async {
        let newest = String(repeating: "A whole paragraph.\n", count: 100)
        let engine = ScriptedControlPlane([
            .brief: .success(
                desktopReply(
                    .brief,
                    [
                        "session": [
                            "name": "A Session", "state_word": "waiting for your decision",
                            "newest": ["text": newest],
                            "decision": [
                                "prompt": "Which one?",
                                "options": [
                                    ["text": "First", "description": "One"],
                                    ["text": "Second", "description": "Two"],
                                ],
                            ],
                        ]
                    ]))
        ])
        let panel = ControlPanel(client: engine)
        await panel.refreshSession(SessionAddress(.of(["agent": "codex", "session_id": "one"])))
        #expect(panel.sessionBrief?.newest == newest)
        #expect(panel.sessionBrief?.stateWord == "waiting for your decision")
        #expect(panel.sessionBrief?.prompt == "Which one?")
        #expect(panel.sessionBrief?.options == ["1. First — One", "2. Second — Two"])
    }

    @Test func newAgentBetweenCallsOnlyForgetsTheOldOne() async {
        let engine = ScriptedControlPlane([
            .forgetCallAgent: .success(desktopReply(.forgetCallAgent, [:])),
            .status: .success(desktopReply(.status, [:])),
        ])
        let panel = ControlPanel(client: engine)
        await panel.newCallAgent()
        #expect(engine.requests() == ["forget_call_agent", "status"])
        #expect(panel.nextCallStartsFresh)
    }

    @Test func callPhasesUseThePressAndReplyTimesAndStatusWinsAfterwards() async throws {
        var time: TimeInterval = 100
        let engine = HeldLivePlane()
        let panel = ControlPanel(client: engine, now: { time })
        await panel.refresh()
        let dial = Task { await panel.toggleLive() }
        try #require(await engine.waitForRequest())
        #expect(panel.phase == .calling)
        time += 44
        panel.tick()
        #expect(panel.elapsed == 44)
        await panel.refresh()
        #expect(panel.phase == .calling)
        try await engine.finish(up: true)
        await dial.value
        #expect(panel.phase == .onCall)
        #expect(panel.elapsed == 0)
        time += 7
        panel.tick()
        #expect(panel.elapsed == 7)
        let hangup = Task { await panel.toggleLive() }
        try #require(await engine.waitForRequest())
        #expect(panel.phase == .ending)
        #expect(panel.elapsed == nil)
        try await engine.finish(up: false)
        await hangup.value
        #expect(panel.phase == .ready)
    }

    @Test func aFailedDialStaysVisibleForSixSeconds() async {
        var time: TimeInterval = 0
        let engine = ScriptedControlPlane([
            .status: .success(desktopReply(.status, [:])),
            .live: .failure(.engineUnreachable("no reply")),
        ])
        let panel = ControlPanel(client: engine, now: { time })
        await panel.toggleLive()
        #expect(panel.phase == .couldNotConnect)
        time = 5
        await panel.refresh()
        panel.tick()
        #expect(panel.phase == .couldNotConnect)
        time = 6
        panel.tick()
        #expect(panel.phase == .ready)
    }

    @Test func onlyTheHomeListHoldsIncomingRowsUntilThePointerLeaves() async {
        let engine = ScriptedControlPlane([
            .brief: .success(
                desktopReply(
                    .brief, ["roster": ["rows": [desktopRow("First"), desktopRow("Second")]]]))
        ])
        let panel = ControlPanel(client: engine)
        await panel.refreshRoster()
        panel.setPointerInRoster(true)
        engine.answer(
            .brief,
            with: .success(
                desktopReply(
                    .brief, ["roster": ["rows": [desktopRow("Second"), desktopRow("First")]]])))
        await panel.refreshRoster()

        #expect(panel.displayedRoster.map(\.name) == ["First", "Second"])
        #expect(panel.firstCountedRow?.name == "Second")
        #expect(panel.firstCountedRow?.stateWord == "waiting for your decision")
        panel.setPointerInRoster(false)
        #expect(panel.displayedRoster.map(\.name) == ["Second", "First"])
    }
}

actor HeldLivePlane: ControlPlaneDialing {
    private var continuation: CheckedContinuation<Reply, any Error>?
    private var up = false
    func waitForRequest() async -> Bool {
        let deadline = Date().addingTimeInterval(3)
        while continuation == nil, Date() < deadline { await Task.yield() }
        return continuation != nil
    }

    func ask(_ request: Request) async throws -> Reply {
        if request.action == .live {
            return try await withCheckedThrowingContinuation { continuation = $0 }
        }
        return try Reply.of(Data(desktopReply(.status, up ? ["call_id": "a-call"] : [:]).utf8))
    }

    func finish(up: Bool) throws {
        self.up = up
        continuation?.resume(
            returning: try Reply.of(
                Data(
                    desktopReply(
                        .live,
                        [
                            "state": up ? "up" : "down", "call_id": up ? "a-call" : NSNull(),
                        ]
                    ).utf8)))
        continuation = nil
    }
}
