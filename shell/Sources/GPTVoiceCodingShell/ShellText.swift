import Foundation
import ShellCore

/// The shell's vocabulary only. Session state words belong to Core.
enum Copy: String, CaseIterable {
    case ready
    case calling
    case onCall
    case ending
    case couldNotConnect
    case callFailed
    case voiceOff
    case duty
    case voice
    case telegramOn
    case telegramOff
    case waitingCount
    case finishedCount
    case call
    case hangUp
    case home
    case messageJustNow, messageSecondsAgo, messageMinutesAgo, messageHoursAgo, messageDaysAgo
    case settings
    case quit
    case hangupAsk
    case quitAsk
    case keepCall
    case notWatching
    case engineDown
    case lampEngineDown
    case openDiagnostics
    case callID
    case callAgent
    case agentLabel
    case context
    case tokens
    case total
    case lastTurn
    case newAgent
    case newAgentAsk
    case keepAgent
    case confirmAgent
    case freshAgent
    case noAgent
    case connectSettings
    case sessions
    case waiting
    case finished
    case children
    case rosterTitle
    case emptyTitle
    case emptyBody
    case back
    case session
    case newest
    case pending
    case answerElsewhere
    case callSettings
    case general
    case telegram
    case diagnostics
    case placeholder
    case engineNotStarted, engineRunning, engineRestarting, engineRepeatedFailures
    case engineAlreadyRunning, engineCannotStart, engineStopped
    case verify
    case checking
    case verifyHint
    case verifyLimit
    case open
    case output
    case copyDiagnostics
    case somethingOff
    case unreadableSettings
    case appVersion
    case engineVersion
    case codexVersion
    case realtimeModel
    case socket
    case log
    case unavailable
    case notChosen
    case loading
    case sessionUnavailable
    case controlPanel
    case model
    case effort
    case voiceNote, settingsSaveFailed, restartRequired, restartNow, restartAfterCall
    case autoHangup, silenceSeconds, coolDownSeconds, speechSettleSeconds, secondsUnit,
        numberRequired
    case appearance, appearanceSystem, appearanceDark, appearanceLight
    case appearanceSystemHint, appearanceDarkHint, appearanceLightHint
    case launchAtLogin, language
    case agentModelsNote, agentModelGone, agentModelsUnavailable
    case onboardingStep, welcomeTitle, welcomeBody, start, codexCheckTitle, codexReady,
        codexNotInstalled, codexInstallFix, codexNotLoggedIn, codexLoginFix, recheck,
        continueSetup, skipSetup, placedTitle, claudePlaced, claudeNotPlaced, codexPlaced,
        codexNotPlaced, placementFailed, placementReversible, folderAccessNotice,
        setupAgentBody, setupTelegramBody, testCallTitle, testCallBody, placeTestCall, doneSetup
    case telegramToken, telegramTokenHelp, telegramValidate, telegramCheck, telegramNamed,
        telegramWaiting, telegramNotFound, telegramConfirmed, telegramInvalidToken,
        telegramConnectionFailed,
        telegramDestinationFailed, telegramMaskedNote, telegramChange, telegramUnbind,
        telegramEngineDown, save, cancel
}

enum ShellLanguage: String, CaseIterable {
    case english = "en"
    case chinese = "zh-Hans"
    var label: String {
        switch self {
        case .english: return "English"
        case .chinese: return "中文"
        }
    }
}

struct ShellText {
    static let preferenceKey = "language"
    let language: String
    private let bundle: Bundle

    init(
        language: String = ShellText.preferredLanguage(
            saved: UserDefaults.standard.string(forKey: preferenceKey),
            system: Locale.preferredLanguages)
    ) {
        self.language = language
        bundle = Bundle(
            url: Bundle.shell.bundleURL.appendingPathComponent("\(language.lowercased()).lproj"))!
    }

    static func preferredLanguage(saved: String?, system: [String]) -> String {
        let language = saved ?? system.first ?? "en"
        return (language.hasPrefix("zh") ? ShellLanguage.chinese : .english).rawValue
    }

    func messageAge(_ message: Date?, at now: Date, long: Bool = false) -> String {
        guard let message else { return "—" }
        let seconds = max(0, Int(now.timeIntervalSince(message)))
        if seconds < 10 { return long ? self(.messageJustNow) : "now" }
        let amount: Int
        let unit: String
        let wording: Copy
        if seconds < 60 {
            (amount, unit, wording) = (seconds, "s", .messageSecondsAgo)
        } else if seconds < 3600 {
            (amount, unit, wording) = (seconds / 60, "m", .messageMinutesAgo)
        } else if seconds < 86400 {
            (amount, unit, wording) = (seconds / 3600, "h", .messageHoursAgo)
        } else {
            (amount, unit, wording) = (seconds / 86400, "d", .messageDaysAgo)
        }
        return long ? self(wording, amount) : "\(amount)\(unit)"
    }

    func callAsFunction(_ key: Copy, _ arguments: CVarArg...) -> String {
        String(
            format: bundle.localizedString(forKey: key.rawValue, value: nil, table: nil),
            locale: Locale(identifier: language), arguments: arguments)
    }

    func name(_ name: String?) -> String { name ?? self(.session) }

    func phase(_ phase: CallPhase) -> String {
        switch phase {
        case .ready: return self(.ready)
        case .calling: return self(.calling)
        case .onCall: return self(.onCall)
        case .ending: return self(.ending)
        case .couldNotConnect: return self(.couldNotConnect)
        }
    }
}
