import AppKit
import SwiftUI

/// Named values transcribed from the handoff's phosphor/tokens.css.
enum Phosphor {
    static let messageTimeWidth: CGFloat = 34
    static let windowWidth: CGFloat = 480
    static let cardWidth: CGFloat = 72
    static let lampHeight: CGFloat = 26
    static let lampCell: CGFloat = 28
    static let lampSlot: CGFloat = cardWidth - lampCell - lampEdge * 2
    static let lampEdge: CGFloat = 1.5
    static let lampRadius: CGFloat = 7
    static let lampGlyph: CGFloat = 15
    static let bubbleWidth: CGFloat = 280
    static let bubbleGap: CGFloat = 8
    static let actionGap: CGFloat = 6
    static let bubbleHold: TimeInterval = 5
    static let hangupHold: TimeInterval = 6
    static let lampPressedScale: CGFloat = 0.92
    static let failedHold: TimeInterval = 6
    static let slotHold: Int = 3
    static let lampPulse: TimeInterval = 1.6
    // The handoff keeps halo RGBA values constant in both appearances.
    static let lampGlow = Color(red: 233.0 / 255, green: 162.0 / 255, blue: 59.0 / 255).opacity(
        0.20)
    static let lampFailureGlow = Color(red: 240.0 / 255, green: 119.0 / 255, blue: 106.0 / 255)
        .opacity(0.22)
    static let bubbleFade: TimeInterval = 0.2
    static let navigationWidth: CGFloat = 126
    static let rosterHeight: CGFloat = 220
    static let outputHeight: CGFloat = 120
    static let padding: CGFloat = 12
    static let rowMark: CGFloat = 16
    static let briefMark: CGFloat = 20
    static let controlRadius: CGFloat = 4
    static let cardRadius: CGFloat = 8
    static let windowRadius: CGFloat = 10
    static let window = colour(light: 0xf7f7f8, dark: 0x000000)
    static let card = colour(light: 0xffffff, dark: 0x0a0a0a)
    static let raised = colour(light: 0xffffff, dark: 0x121212)
    static let sunken = colour(light: 0xececf1, dark: 0x000000)
    static let primary = colour(light: 0x0d0d0d, dark: 0xececf1)
    static let secondary = colour(light: 0x4d4d56, dark: 0xb0b0bd)
    static let tertiary = colour(light: 0x6b6b78, dark: 0x8e8ea0)
    static let muted = colour(light: 0x8e8ea0, dark: 0x6b6b78)
    static let bright = colour(light: 0x6b3f05, dark: 0xf4e3c4)
    static let accent = colour(light: 0x9a5c06, dark: 0xe9a23b)
    static let accentInk = colour(light: 0xffffff, dark: 0x000000)
    static let danger = colour(light: 0xc7362b, dark: 0xf0776a)
    static let rule = colour(light: 0x0d0d0d, dark: 0xffffff).opacity(0.14)
    static let ruleStrong = colour(
        light: 0x0d0d0d, dark: 0xffffff, lightAlpha: 0.26, darkAlpha: 0.24)
    static let accentWash = colour(
        light: 0x9a5c06, dark: 0xe9a23b, lightAlpha: 0.10, darkAlpha: 0.12)
    static let ruleAccent = accent.opacity(0.45)
    static let dangerInk = colour(light: 0xffffff, dark: 0x000000)
    static let switchWidth: CGFloat = 30
    static let switchHeight: CGFloat = 16
    static let switchKnob: CGFloat = 12
    static let body = mono(size: 13)
    static let small = mono(size: 12)
    static let title = mono(size: 22, weight: .medium)
    static let display = mono(size: 28, weight: .medium)
    static let prose = Font.system(size: 13)
    static let label = mono(size: 11, weight: .medium)
    static let headline = mono(size: 15, weight: .medium)
    static let motion = Animation.timingCurve(0.32, 0.72, 0, 1, duration: 0.2)
    static let pulse = Animation.easeInOut(duration: 0.8)

    static func state(_ state: String) -> Color {
        switch state {
        case "decision", "permission": return accent
        case "waiting_on": return secondary
        case "finished": return tertiary
        default: return bright
        }
    }

    private static func colour(
        light: UInt32, dark: UInt32, lightAlpha: Double = 1, darkAlpha: Double = 1
    ) -> Color {
        Color(
            nsColor: NSColor(name: nil) { appearance in
                let isDark = appearance.bestMatch(from: [.aqua, .darkAqua]) == .darkAqua
                let rgb = isDark ? dark : light
                return NSColor(
                    srgbRed: Double((rgb >> 16) & 255) / 255,
                    green: Double((rgb >> 8) & 255) / 255, blue: Double(rgb & 255) / 255,
                    alpha: isDark ? darkAlpha : lightAlpha)
            })
    }

    private static func mono(size: CGFloat, weight: NSFont.Weight = .regular) -> Font {
        let face = weight == .medium ? "JetBrainsMono-Medium" : "JetBrainsMono-Regular"
        return Font(
            NSFont(name: face, size: size)
                ?? NSFont.monospacedSystemFont(ofSize: size, weight: weight))
    }
}

enum DesignMark: String, CaseIterable {
    case app = "app-icon"
    case menu = "menubar-icon"
    case claude = "claude-mono"
    case codex = "codex-mono"

    @MainActor var image: NSImage {
        let image = NSImage(
            contentsOf: Bundle.shell.url(forResource: rawValue, withExtension: "svg")!)!
        image.isTemplate = self != .app
        return image
    }
}

struct AgentMark: View {
    let agent: String
    var size = Phosphor.rowMark
    var body: some View {
        Image(nsImage: (agent == "claude" ? DesignMark.claude : .codex).image)
            .resizable().scaledToFit().frame(width: size, height: size)
            .foregroundStyle(Phosphor.secondary).accessibilityHidden(true)
    }
}

struct ConsoleButton: ButtonStyle {
    var prominent = false
    var destructive = false
    var large = false
    @Environment(\.isEnabled) private var isEnabled
    func makeBody(configuration: Configuration) -> some View {
        configuration.label.font(Phosphor.body)
            .padding(.horizontal, large ? 16 : prominent ? 14 : 10)
            .padding(.vertical, large ? 6 : prominent ? 5 : 4)
            .foregroundStyle(
                !isEnabled
                    ? Phosphor.muted
                    : destructive
                        ? Phosphor.danger
                        : prominent ? Phosphor.accentInk : Phosphor.primary
            )
            .background(
                prominent && isEnabled ? Phosphor.accent : Phosphor.raised,
                in: RoundedRectangle(cornerRadius: Phosphor.controlRadius)
            )
            .overlay(
                RoundedRectangle(cornerRadius: Phosphor.controlRadius)
                    .stroke(
                        destructive ? Phosphor.danger : prominent ? .clear : Phosphor.ruleStrong)
            )
            .opacity(configuration.isPressed ? 0.7 : 1).pointingHand()
    }
}

struct ConsoleToggle: ToggleStyle {
    var compact = false
    var hidesLabel = false
    @Environment(\.isEnabled) private var isEnabled

    func makeBody(configuration: Configuration) -> some View {
        Button {
            configuration.isOn.toggle()
        } label: {
            HStack(spacing: 6) {
                if !hidesLabel {
                    Text("›")
                        .foregroundStyle(
                            configuration.isOn && isEnabled ? Phosphor.accent : Phosphor.muted)
                    configuration.label
                    if !compact { Spacer(minLength: 8) }
                }
                Capsule()
                    .fill(configuration.isOn && isEnabled ? Phosphor.accent : Color.clear)
                    .overlay(
                        Capsule().stroke(
                            isEnabled ? Phosphor.ruleStrong : Phosphor.rule,
                            style: StrokeStyle(lineWidth: 1, dash: isEnabled ? [] : [3, 3]))
                    )
                    .overlay(alignment: configuration.isOn && isEnabled ? .trailing : .leading) {
                        Circle()
                            .fill(
                                configuration.isOn && isEnabled
                                    ? Phosphor.accentInk : Phosphor.tertiary
                            )
                            .frame(width: Phosphor.switchKnob, height: Phosphor.switchKnob)
                            .padding(2)
                    }
                    .frame(width: Phosphor.switchWidth, height: Phosphor.switchHeight)
            }
            .frame(minHeight: 24)
            .contentShape(Rectangle())
            .opacity(isEnabled ? 1 : 0.5)
        }
        .buttonStyle(PlainHandButton())
        .accessibilityRepresentation {
            Toggle(isOn: configuration.$isOn) { configuration.label }.toggleStyle(.switch)
        }
    }
}

struct ConsoleRule: View {
    var body: some View {
        Line().stroke(Phosphor.rule, style: StrokeStyle(lineWidth: 1, dash: [3, 3]))
            .frame(height: 1)
    }

    private struct Line: Shape {
        func path(in rect: CGRect) -> Path {
            Path { path in
                path.move(to: CGPoint(x: rect.minX, y: rect.midY))
                path.addLine(to: CGPoint(x: rect.maxX, y: rect.midY))
            }
        }
    }
}

extension View {
    func consoleCard() -> some View {
        padding(Phosphor.padding)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(Phosphor.card, in: RoundedRectangle(cornerRadius: Phosphor.cardRadius))
            .overlay(
                RoundedRectangle(cornerRadius: Phosphor.cardRadius)
                    .stroke(Phosphor.rule, style: StrokeStyle(lineWidth: 1, dash: [3, 3])))
    }
}
