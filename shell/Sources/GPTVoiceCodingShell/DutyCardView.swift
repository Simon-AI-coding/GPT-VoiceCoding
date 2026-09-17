import AppKit
import ShellCore
import SwiftUI

/// The handoff's fixed plate. AppKit supplies the pointing hand and drag/click handling.
struct DutyCardView: View {
    @Bindable var shell: ShellModel
    private var failed: Bool { shell.panel.phase == .couldNotConnect }
    private var watching: Bool { shell.panel.engineReachable }
    private var ending: Bool { shell.panel.phase == .ending }
    private var lit: Bool {
        watching && !failed && !ending
            && (shell.panel.counts.waiting > 0 || shell.panel.phase == .calling
                || shell.panel.phase == .onCall)
    }
    private var edge: Color {
        !watching
            ? Phosphor.muted
            : failed ? Phosphor.danger : lit ? Phosphor.accent : Phosphor.ruleStrong
    }
    private var cellFill: Color {
        if shell.lampHangupArmed { return Phosphor.danger }
        return !watching
            ? .clear
            : failed
                ? Phosphor.danger
                : lit ? Phosphor.accent : ending ? Phosphor.tertiary : Phosphor.raised
    }
    private var pulses: Bool { watching && (shell.panel.phase == .calling || ending) }
    private var glyph: LampGlyph {
        if !watching { return .off }
        if shell.lampHangupArmed { return .ending }
        switch shell.panel.phase {
        case .ready: return .ready
        case .calling: return .calling
        case .onCall: return .live
        case .ending: return .ending
        case .couldNotConnect: return .failed
        }
    }
    private var glyphInk: Color {
        if shell.lampHangupArmed { return Phosphor.dangerInk }
        return !watching
            ? Phosphor.muted
            : failed
                ? Phosphor.dangerInk
                : ending ? Phosphor.sunken : lit ? Phosphor.accentInk : Phosphor.secondary
    }
    private var multipleAgents: Bool {
        watching
            && Set(shell.panel.roster.filter { $0.state != "finished" }.map { $0.target.agent })
                .count > 1
    }

    var body: some View {
        HStack(spacing: 0) {
            if pulses {
                cell.phaseAnimator([1.0, 0.3]) { content, opacity in
                    content.opacity(opacity)
                } animation: { _ in
                    .timingCurve(0.32, 0.72, 0, 1, duration: Phosphor.lampPulse / 2)
                }
            } else {
                cell
            }
            slot.frame(width: Phosphor.lampSlot).pointingHand()
        }
        .frame(height: Phosphor.lampHeight - Phosphor.lampEdge * 2)
        .padding(Phosphor.lampEdge)
        .background(Phosphor.sunken)
        .clipShape(RoundedRectangle(cornerRadius: Phosphor.lampRadius))
        .overlay(
            RoundedRectangle(cornerRadius: Phosphor.lampRadius).strokeBorder(
                edge,
                style: StrokeStyle(lineWidth: Phosphor.lampEdge, dash: watching ? [] : [3, 3]))
        )
        .overlay(
            RoundedRectangle(cornerRadius: Phosphor.lampRadius + 1.5)
                .stroke(
                    lit ? Phosphor.lampGlow : failed ? Phosphor.lampFailureGlow : .clear,
                    lineWidth: 3
                ).padding(-1.5)
        )
        .modifier(LampShadow(enabled: watching))
        .frame(width: Phosphor.cardWidth, height: Phosphor.lampHeight)
        .accessibilityElement(children: .combine)
        .accessibilityLabel(shell.text(phaseCopy))
        .accessibilityValue(
            shell.text(.waitingCount, shell.panel.counts.waiting) + ", "
                + shell.text(.finishedCount, shell.panel.counts.finished)
        )
        .accessibilityAction { shell.toggleControlPanel() }
    }

    private var phaseCopy: Copy {
        guard watching else { return .notWatching }
        switch shell.panel.phase {
        case .ready: return .ready
        case .calling: return .calling
        case .onCall: return .onCall
        case .ending: return .ending
        case .couldNotConnect: return .couldNotConnect
        }
    }

    private var cell: some View {
        ZStack(alignment: .bottom) {
            cellFill
            Group {
                if glyph == .live {
                    HStack(spacing: 1.5) {
                        ForEach([7.0, 13.0, 9.0], id: \.self) { height in
                            RoundedRectangle(cornerRadius: 2).fill(glyphInk).frame(
                                width: 2.5, height: height)
                        }
                    }
                } else {
                    Image(nsImage: glyph.image!).resizable().scaledToFit()
                        .foregroundStyle(glyphInk).frame(
                            width: Phosphor.lampGlyph, height: Phosphor.lampGlyph)
                }
            }.frame(maxWidth: .infinity, maxHeight: .infinity)
            if multipleAgents {
                HStack(spacing: 0) {
                    Phosphor.bright
                    Phosphor.secondary
                }.frame(height: 2)
            }
        }.frame(width: Phosphor.lampCell)
            .scaleEffect(shell.lampCellPressed ? Phosphor.lampPressedScale : 1)
            .pointingHand(enabled: shell.lampCellEnabled)
    }

    @ViewBuilder private var slot: some View {
        if !watching {
            Text("—").foregroundStyle(Phosphor.muted).font(Phosphor.small)
        } else if failed {
            Text("!").foregroundStyle(Phosphor.danger).font(Phosphor.small.bold())
        } else if let time = shell.lampTime {
            Text(time).foregroundStyle(Phosphor.bright).font(Phosphor.small.weight(.medium))
                .lineLimit(1).minimumScaleFactor(0.7)
        } else if shell.panel.counts.waiting + shell.panel.counts.finished == 0 {
            Text("—").foregroundStyle(Phosphor.muted).font(Phosphor.small)
        } else {
            HStack(spacing: 1) {
                Text(count(shell.panel.counts.waiting))
                    .foregroundStyle(
                        shell.panel.counts.waiting > 0 ? Phosphor.accent : Phosphor.muted
                    )
                    .font(Phosphor.small.weight(shell.panel.counts.waiting > 0 ? .bold : .medium))
                if shell.panel.counts.finished > 0 {
                    Text("/").foregroundStyle(Phosphor.muted).font(Phosphor.small)
                    Text(count(shell.panel.counts.finished)).foregroundStyle(Phosphor.tertiary)
                        .font(Phosphor.small.weight(.medium))
                }
            }.fixedSize()
        }
    }
    private func count(_ value: Int) -> String { value > 9 ? "9+" : String(value) }
}

@MainActor
enum LampGlyph: String, CaseIterable {
    case ready, calling, live, ending, failed, off
    private static let images: [LampGlyph: NSImage] = Dictionary(
        uniqueKeysWithValues: allCases.filter { $0 != .live }.map { glyph in
            let image = NSImage(
                contentsOf: Bundle.shell.url(
                    forResource: "lamp-" + glyph.rawValue, withExtension: "svg")!)!
            image.isTemplate = true
            return (glyph, image)
        })
    var image: NSImage? { Self.images[self] }
}

struct LampShadow: ViewModifier {
    var enabled = true
    @Environment(\.colorScheme) private var scheme
    func body(content: Content) -> some View {
        content.shadow(
            color: .black.opacity(enabled ? (scheme == .dark ? 0.65 : 0.14) : 0), radius: 3, x: 0,
            y: 2
        )
        .shadow(
            color: .black.opacity(enabled ? (scheme == .dark ? 0.65 : 0.20) : 0),
            radius: scheme == .dark ? 28 : 24,
            x: 0, y: scheme == .dark ? 20 : 16)
    }
}

struct LampActionsView: View {
    let shell: ShellModel
    var body: some View {
        HStack(spacing: 0) {
            if shell.cardQuitVisible {
                action(.quit, ink: Phosphor.danger) {
                    shell.quit(fromLamp: true)
                    shell.dismissLampActions()
                }
            } else {
                Button {
                    Task { await shell.panel.toggleLive() }
                } label: {
                    // Both labels participate in sizing, so the strip's anchor never jumps.
                    ZStack {
                        Text(shell.text(.call)).hidden()
                        Text(shell.text(.hangUp)).hidden()
                        Text(shell.text(shell.panel.phase == .onCall ? .hangUp : .call))
                    }.font(Phosphor.small.weight(.medium)).padding(.horizontal, 10)
                        .frame(height: Phosphor.lampHeight).contentShape(Rectangle())
                }.buttonStyle(PlainHandButton())
                    .foregroundStyle(
                        shell.panel.phase.resolving || shell.panel.busy
                            || !shell.panel.engineReachable
                            ? Phosphor.muted
                            : shell.panel.phase == .onCall ? Phosphor.danger : Phosphor.accent
                    )
                    .disabled(
                        shell.panel.phase.resolving || shell.panel.busy
                            || !shell.panel.engineReachable)
                divider
                action(.home) {
                    shell.open(.home)
                    shell.dismissLampActions()
                }
                divider
                action(.settings) {
                    shell.open(.settings(.voice))
                    shell.dismissLampActions()
                }
            }
        }.fixedSize(horizontal: true, vertical: true).frame(height: Phosphor.lampHeight)
            .background(Phosphor.window, in: RoundedRectangle(cornerRadius: Phosphor.lampRadius))
            .overlay(
                RoundedRectangle(cornerRadius: Phosphor.lampRadius)
                    .strokeBorder(shell.cardQuitVisible ? Phosphor.danger : Phosphor.ruleStrong)
            )
            .modifier(LampShadow())
    }
    private var divider: some View { Rectangle().fill(Phosphor.rule).frame(width: 1, height: 14) }
    private func action(
        _ copy: Copy, ink: Color = Phosphor.secondary, perform: @escaping () -> Void
    ) -> some View {
        Button(action: perform) {
            Text(shell.text(copy)).font(Phosphor.small).padding(.horizontal, 10)
                .frame(height: Phosphor.lampHeight).contentShape(Rectangle())
        }
        .buttonStyle(PlainHandButton()).foregroundStyle(ink)
    }
}

struct LampBubbleView: View {
    let shell: ShellModel
    let bubble: LampBubble
    private var border: Color {
        switch bubble {
        case .session(_, let automatically):
            return automatically ? Phosphor.ruleStrong : Phosphor.rule
        case .failure, .confirmation: return Phosphor.danger
        case .engine: return Phosphor.ruleStrong
        }
    }
    var body: some View {
        if let row = bubble.row {
            Button {
                shell.openLampBrief(row.target)
            } label: {
                surface.contentShape(Rectangle())
            }
            .buttonStyle(PlainHandButton())
        } else {
            surface
        }
    }
    private var verticalPadding: CGFloat {
        if case .session(_, automatically: false) = bubble { return 7 }
        return 8
    }
    private var surface: some View {
        Group {
            switch bubble {
            case .session(let row, _):
                HStack(alignment: .top, spacing: 8) {
                    AgentMark(agent: row.target.agent).padding(.top, 2)
                    VStack(alignment: .leading, spacing: 2) {
                        HStack(alignment: .firstTextBaseline, spacing: 6) {
                            Text(shell.text.name(row.name)).font(Phosphor.small.weight(.medium))
                                .foregroundStyle(Phosphor.primary).lineLimit(1)
                            Text(row.stateWord).font(Phosphor.small).foregroundStyle(
                                Phosphor.state(row.state)
                            ).lineLimit(1)
                            Spacer(minLength: 0)
                            Text(shell.text.messageAge(row.messageAt, at: shell.messageNow))
                                .font(Phosphor.small).monospacedDigit().foregroundStyle(
                                    Phosphor.muted
                                )
                                .fixedSize().layoutPriority(2)
                        }
                        Text(row.newest).font(Phosphor.prose).foregroundStyle(Phosphor.tertiary)
                            .lineLimit(3).multilineTextAlignment(.leading)
                    }.frame(maxWidth: .infinity, alignment: .leading)
                }
            case .failure:
                VStack(alignment: .leading, spacing: 2) {
                    Text(shell.text(.couldNotConnect)).font(Phosphor.small.weight(.medium))
                        .foregroundStyle(Phosphor.danger)
                    Text(shell.text(.callFailed)).font(Phosphor.prose).foregroundStyle(
                        Phosphor.tertiary)
                }
            case .engine:
                HStack(spacing: 8) {
                    Text(shell.text(.lampEngineDown)).font(Phosphor.prose).foregroundStyle(
                        Phosphor.secondary)
                    Button(shell.text(.diagnostics)) {
                        shell.open(.settings(.diagnostics))
                    }
                    .font(Phosphor.small).buttonStyle(PlainHandButton()).foregroundStyle(
                        Phosphor.accent
                    )
                    .fixedSize()
                }
            case .confirmation:
                VStack(alignment: .leading, spacing: 8) {
                    Text(shell.text(shell.confirmation == .hangUp ? .hangupAsk : .quitAsk)).font(
                        Phosphor.prose
                    ).foregroundStyle(
                        Phosphor.secondary)
                    HStack(spacing: 6) {
                        Button(shell.text(.keepCall)) {
                            Task { await shell.resolveConfirmation(accept: false) }
                        }
                        .buttonStyle(LampConfirmationButton()).keyboardShortcut(.defaultAction)
                        Button(shell.text(shell.confirmation == .hangUp ? .hangUp : .quit)) {
                            Task { await shell.resolveConfirmation(accept: true) }
                        }
                        .buttonStyle(LampConfirmationButton(destructive: true))
                    }
                }
            }
        }.padding(.horizontal, 10).padding(.vertical, verticalPadding)
            .frame(width: Phosphor.bubbleWidth, alignment: .leading)
            .background(Phosphor.window, in: RoundedRectangle(cornerRadius: Phosphor.cardRadius))
            .overlay(
                RoundedRectangle(cornerRadius: Phosphor.cardRadius).strokeBorder(
                    border,
                    style: StrokeStyle(lineWidth: 1, dash: bubble == .engine ? [3, 3] : []))
            )
            .modifier(LampShadow())
    }
}

private struct LampConfirmationButton: ButtonStyle {
    var destructive = false
    func makeBody(configuration: Configuration) -> some View {
        configuration.label.font(Phosphor.small.weight(destructive ? .medium : .regular))
            .padding(.horizontal, 10).padding(.vertical, 3)
            .foregroundStyle(destructive ? Phosphor.dangerInk : Phosphor.primary)
            .background(
                destructive ? Phosphor.danger : .clear,
                in: RoundedRectangle(cornerRadius: Phosphor.controlRadius)
            )
            .overlay(
                RoundedRectangle(cornerRadius: Phosphor.controlRadius)
                    .strokeBorder(destructive ? .clear : Phosphor.ruleStrong)
            )
            .opacity(configuration.isPressed ? 0.7 : 1).pointingHand()
    }
}
