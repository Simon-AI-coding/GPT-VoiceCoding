import AppKit
import SwiftUI

/// Named values transcribed from the handoff's phosphor/tokens.css.
enum Phosphor {
    static let windowWidth: CGFloat = 480
    static let cardWidth: CGFloat = 352
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
    static let ruleStrong = colour(light: 0x0d0d0d, dark: 0xffffff).opacity(0.25)
    static let switchWidth: CGFloat = 30
    static let switchHeight: CGFloat = 16
    static let switchKnob: CGFloat = 12
    static let body = Font.system(size: 13, design: .monospaced)
    static let small = Font.system(size: 12, design: .monospaced)
    static let title = Font.system(size: 22, weight: .medium, design: .monospaced)
    static let display = Font.system(size: 28, weight: .medium, design: .monospaced)
    static let prose = Font.system(size: 13)
    static let label = Font.system(size: 11, weight: .medium, design: .monospaced)
    static let headline = Font.system(size: 15, weight: .medium, design: .monospaced)
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

    private static func colour(light: UInt32, dark: UInt32) -> Color {
        Color(
            nsColor: NSColor(name: nil) { appearance in
                let rgb =
                    appearance.bestMatch(from: [.aqua, .darkAqua]) == .darkAqua ? dark : light
                return NSColor(
                    srgbRed: Double((rgb >> 16) & 255) / 255,
                    green: Double((rgb >> 8) & 255) / 255, blue: Double(rgb & 255) / 255, alpha: 1)
            })
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
    @Environment(\.isEnabled) private var isEnabled
    func makeBody(configuration: Configuration) -> some View {
        configuration.label.font(Phosphor.body)
            .padding(.horizontal, 10).padding(.vertical, 4)
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
                    .stroke(destructive ? Phosphor.danger : Phosphor.ruleStrong)
            )
            .opacity(configuration.isPressed ? 0.7 : 1)
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
                    .overlay(Capsule().stroke(isEnabled ? Phosphor.ruleStrong : Phosphor.rule))
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
        .buttonStyle(.plain)
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
