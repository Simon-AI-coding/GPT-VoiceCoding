import AppKit
import Observation
import SwiftUI

final class DutyPanel: NSPanel {
    static let autosaveName = "DutyCard"
    static let lampLevel = NSWindow.Level.floating
    private let savedFrameName: String
    private var placed = false
    var cancelConfirmation: (() -> Void)?

    init(savedFrameName: String = DutyPanel.autosaveName) {
        self.savedFrameName = savedFrameName
        super.init(
            contentRect: NSRect(x: 0, y: 0, width: Phosphor.cardWidth, height: Phosphor.lampHeight),
            styleMask: [.borderless, .nonactivatingPanel], backing: .buffered, defer: true)
        level = Self.lampLevel
        collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
        hidesOnDeactivate = false
        isMovableByWindowBackground = true
        isReleasedWhenClosed = false
        isOpaque = false
        backgroundColor = .clear
        hasShadow = true
        acceptsMouseMovedEvents = true
    }

    override var canBecomeKey: Bool { cancelConfirmation != nil }
    override func cancelOperation(_ sender: Any?) { cancelConfirmation?() }
    override var canBecomeMain: Bool { false }

    func place(in screen: NSRect) {
        guard !placed else { return }
        // Enabling autosave can restore a frame itself. Do it before normalization.
        setFrameAutosaveName(savedFrameName)
        if !setFrameUsingName(savedFrameName, force: true) {
            setFrame(Self.initialFrame(in: screen, height: frame.height), display: false)
        }
        // A saved old card contributes its top-right anchor, never its old size.
        setFrame(
            NSRect(
                x: frame.maxX - Phosphor.cardWidth, y: frame.maxY - Phosphor.lampHeight,
                width: Phosphor.cardWidth, height: Phosphor.lampHeight), display: false)
        placed = true
    }

    static func initialFrame(in screen: NSRect, height: CGFloat) -> NSRect {
        NSRect(
            x: screen.maxX - 16 - Phosphor.cardWidth,
            y: screen.maxY - 8 - height, width: Phosphor.cardWidth, height: height)
    }
}

/// AppKit owns window mechanics; every displayed fact remains in ShellModel.
@MainActor
final class DesktopWindows: NSObject, NSWindowDelegate {
    private let shell: ShellModel
    private let card: DutyPanel
    private let actions = DutyPanel()
    private let bubble = DutyPanel()
    private var presentedBubble: LampBubble?
    private let control: ControlWindow
    private var lastWindowRequest = 0
    /// The strip and bubble are withdrawn while the Lamp is dragged, and return where it lands.
    private var lampDragging = false
    private var localClick: Any?
    private var globalClick: Any?
    private var resignActive: NSObjectProtocol?

    init(shell: ShellModel, savedFrameName: String = DutyPanel.autosaveName) {
        self.shell = shell
        self.card = DutyPanel(savedFrameName: savedFrameName)
        self.control = ControlWindow(content: ControlPanelView(shell: shell))
        super.init()
        control.onClose = { [weak self] in self?.shell.closeWindow() }
        let lamp = LampHostingView(rootView: DutyCardView(shell: shell))
        lamp.onDrag = { [weak self] dragging in self?.lampDragged(dragging) }
        card.contentView = lamp
        card.delegate = self
        actions.isMovableByWindowBackground = false
        bubble.isMovableByWindowBackground = false
        card.addChildWindow(actions, ordered: .above)
        card.addChildWindow(bubble, ordered: .above)
        synchronize()
        observe()
        // The open panel keeps the app active; the user going to another app is the outside click.
        resignActive = NotificationCenter.default.addObserver(
            forName: NSApplication.didResignActiveNotification, object: nil, queue: .main
        ) { [weak self] _ in
            MainActor.assumeIsolated {
                guard let self, self.shell.windowOpen else { return }
                self.shell.closeWindow()
            }
        }
    }

    private func observe() {
        withObservationTracking {
            _ = shell.page
            _ = shell.windowRequest
            _ = shell.cardVisible
            _ = shell.cardActionsVisible
            _ = shell.cardQuitVisible
            _ = shell.lampBubble
            _ = shell.panel.phase
            _ = shell.panel.sessionBrief
            _ = shell.panel.sessionFailure
            _ = shell.selectedAppearance
        } onChange: { [weak self] in
            Task { @MainActor in
                self?.synchronize()
                self?.observe()
            }
        }
    }

    /// The windows this object owns, for its tests; nothing finds them through `NSApp.windows`.
    var surfaces: [NSWindow] { [card, actions, bubble, control.window] }

    private func synchronize() {
        for surface in surfaces {
            surface.appearance = shell.selectedAppearance.native
        }

        // The Lamp's place anchors the Control Panel even while Duty hides the Lamp.
        if let screen = NSScreen.main { card.place(in: screen.visibleFrame) }
        if shell.windowOpen {
            if shell.windowRequest != lastWindowRequest {
                lastWindowRequest = shell.windowRequest
                // An open panel stays where it is; navigation keeps its top edge.
                let opening = !control.isVisible
                control.present(
                    anchor: opening ? card.frame : nil,
                    screen: (card.screen ?? NSScreen.main)?.visibleFrame)
                NSApp.activate(ignoringOtherApps: true)
            }
        } else {
            control.dismiss()
        }
        if shell.cardVisible {
            card.hasShadow = shell.panel.engineReachable
            card.orderFrontRegardless()
            if !lampDragging { synchronizeAttachments() }
        } else {
            card.orderOut(nil)
            actions.orderOut(nil)
            bubble.orderOut(nil)
            presentedBubble = nil
            shell.dismissLampActions()
        }
        if shell.cardVisible || shell.windowOpen {
            observeOutsideClicks()
        } else {
            stopObservingClicks()
        }
    }

    private func synchronizeAttachments() {
        if shell.cardActionsVisible || shell.cardQuitVisible {
            let host = NSHostingView(rootView: LampActionsView(shell: shell))
            actions.contentView = host
            actions.setContentSize(host.fittingSize)
            actions.orderFrontRegardless()
        } else {
            actions.orderOut(nil)
        }
        if let content = shell.lampBubble {
            if presentedBubble == .confirmation && content != .confirmation {
                bubble.orderOut(nil)
            }
            if content == .confirmation {
                bubble.cancelConfirmation = { [weak self] in self?.shell.confirmation = nil }
            } else {
                bubble.cancelConfirmation = nil
            }
            if content != presentedBubble {
                presentedBubble = content
                let host = NSHostingView(rootView: LampBubbleView(shell: shell, bubble: content))
                bubble.contentView = host
                bubble.setContentSize(host.fittingSize)
            }
            let entering = !bubble.isVisible
            if entering { bubble.alphaValue = 0 }
            bubble.orderFrontRegardless()
            // Only the explicitly requested confirmation takes keys. Passive briefs never do.
            if content == .confirmation && !bubble.isKeyWindow { bubble.makeKey() }
            if entering {
                NSAnimationContext.runAnimationGroup { context in
                    context.duration = Phosphor.bubbleFade
                    context.timingFunction = CAMediaTimingFunction(controlPoints: 0.32, 0.72, 0, 1)
                    bubble.animator().alphaValue = 1
                }
            }
        } else {
            presentedBubble = nil
            bubble.orderOut(nil)
            bubble.cancelConfirmation = nil
        }
        positionAttachments()
    }

    private func positionAttachments() {
        actions.setFrameOrigin(
            NSPoint(
                x: card.frame.minX - Phosphor.actionGap - actions.frame.width,
                y: card.frame.maxY - Phosphor.lampHeight))
        bubble.setFrameOrigin(
            NSPoint(
                x: card.frame.maxX - bubble.frame.width,
                y: card.frame.minY - Phosphor.bubbleGap - bubble.frame.height))
    }

    func windowDidMove(_ notification: Notification) {
        guard notification.object as? NSWindow === card, !lampDragging else { return }
        positionAttachments()
    }

    private func lampDragged(_ dragging: Bool) {
        lampDragging = dragging
        if dragging {
            actions.orderOut(nil)
            bubble.orderOut(nil)
            presentedBubble = nil
        } else {
            updatePointer()
            synchronize()
        }
    }

    private func updatePointer() {
        guard !lampDragging else { return }
        let point = NSEvent.mouseLocation
        shell.lampCellHovered =
            shell.cardVisible && card.frame.contains(point)
            && point.x < card.frame.minX + Phosphor.lampEdge + Phosphor.lampCell
        shell.setLampPointer(
            inside: shell.cardVisible
                && Self.containsReadingPoint(
                    NSEvent.mouseLocation,
                    lamp: card.frame, actions: actions.isVisible ? actions.frame : nil,
                    bubble: bubble.isVisible ? bubble.frame : nil)
        )
    }

    static func containsReadingPoint(
        _ point: NSPoint, lamp: NSRect, actions: NSRect? = nil, bubble: NSRect?
    ) -> Bool {
        if lamp.contains(point) { return true }
        // The strip, and the gap between it and the Lamp.
        if let actions,
            NSRect(
                x: actions.minX, y: lamp.minY, width: lamp.maxX - actions.minX, height: lamp.height
            )
            .contains(point)
        {
            return true
        }
        guard let bubble else { return false }
        let inBubble = bubble.contains(point)
        let inCrossing =
            NSRect(
                x: bubble.minX,
                y: bubble.maxY, width: bubble.width, height: Phosphor.bubbleGap
            ).contains(point)
        return inBubble || inCrossing
    }

    private func observeOutsideClicks() {
        guard localClick == nil else { return }
        let mask: NSEvent.EventTypeMask = [
            .leftMouseDown, .rightMouseDown, .mouseMoved, .leftMouseDragged, .keyDown,
        ]
        localClick = NSEvent.addLocalMonitorForEvents(matching: mask) { [weak self] event in
            guard let self else { return event }
            if event.type == .mouseMoved || event.type == .leftMouseDragged { self.updatePointer() }
            if event.type == .keyDown && event.keyCode == 53 && self.shell.confirmation != nil {
                self.shell.confirmation = nil
                return nil
            }
            if event.type == .leftMouseDown || event.type == .rightMouseDown {
                if event.window !== self.card && event.window !== self.actions
                    && event.window !== self.bubble
                {
                    self.shell.dismissLampActions()
                }
            }
            return event
        }
        globalClick = NSEvent.addGlobalMonitorForEvents(matching: [
            .leftMouseDown, .rightMouseDown, .mouseMoved, .leftMouseDragged,
        ]) { [weak self] event in
            guard let self else { return }
            if event.type == .mouseMoved || event.type == .leftMouseDragged {
                self.updatePointer()
            } else {
                self.shell.dismissLampActions()
            }
        }
    }

    private func stopObservingClicks() {
        if let localClick { NSEvent.removeMonitor(localClick) }
        if let globalClick { NSEvent.removeMonitor(globalClick) }
        localClick = nil
        globalClick = nil
    }
}

/// Owns the entire plate's hit region, so a text label cannot turn dragging into selection.
@MainActor
final class LampHostingView: NSHostingView<DutyCardView> {
    private let shell: ShellModel
    private var pointerTracking: NSTrackingArea?
    /// Told when a drag of the Lamp begins and when it ends.
    var onDrag: (Bool) -> Void = { _ in }
    required init(rootView: DutyCardView) {
        self.shell = rootView.shell
        super.init(rootView: rootView)
    }
    @available(*, unavailable) required init?(coder: NSCoder) { fatalError() }
    override func hitTest(_ point: NSPoint) -> NSView? { bounds.contains(point) ? self : nil }
    override func acceptsFirstMouse(for event: NSEvent?) -> Bool { true }
    override func updateTrackingAreas() {
        super.updateTrackingAreas()
        if let pointerTracking { removeTrackingArea(pointerTracking) }
        let tracking = NSTrackingArea(
            rect: bounds,
            options: [.activeAlways, .mouseEnteredAndExited, .mouseMoved, .inVisibleRect],
            owner: self)
        addTrackingArea(tracking)
        pointerTracking = tracking
    }
    /// Shown while the app is inactive only because of `BackgroundCursor`.
    func cursor(at point: NSPoint) -> NSCursor {
        let inCell = point.x < Phosphor.lampEdge + Phosphor.lampCell
        return inCell && !shell.lampCellEnabled ? .arrow : .pointingHand
    }
    override func mouseEntered(with event: NSEvent) {
        mouseMoved(with: event)
        shell.setLampPointer(inside: true)
    }
    override func mouseMoved(with event: NSEvent) {
        let point = convert(event.locationInWindow, from: nil)
        let inCell = point.x < Phosphor.lampEdge + Phosphor.lampCell
        shell.lampCellHovered = inCell
        cursor(at: point).set()
    }
    override func mouseExited(with event: NSEvent) {
        shell.lampCellHovered = false
        NSCursor.arrow.set()
    }
    override func rightMouseDown(with event: NSEvent) { shell.toggleLampQuit() }
    override func mouseDown(with event: NSEvent) {
        guard let window else { return }
        let inCell =
            convert(event.locationInWindow, from: nil).x < Phosphor.lampEdge + Phosphor.lampCell
        shell.lampCellPressed = inCell && shell.lampCellEnabled
        defer { shell.lampCellPressed = false }
        // Dragged here rather than by `performDrag`, so the drag has a known end.
        let start = NSEvent.mouseLocation
        let origin = window.frame.origin
        var dragging = false
        while let next = window.nextEvent(matching: [.leftMouseDragged, .leftMouseUp]) {
            if next.type == .leftMouseUp {
                if dragging {
                    onDrag(false)
                } else if inCell {
                    Task { await shell.activateLampCell() }
                } else {
                    shell.toggleControlPanel()
                }
                return
            }
            if !dragging {
                dragging = true
                onDrag(true)
            }
            let point = NSEvent.mouseLocation
            window.setFrameOrigin(
                NSPoint(x: origin.x + point.x - start.x, y: origin.y + point.y - start.y))
        }
    }
}

/// A non-intercepting native tracking region; works while its window is inactive
/// only because of `BackgroundCursor`.
private struct HandRegion: NSViewRepresentable {
    let enabled: Bool
    func makeNSView(context: Context) -> HandTrackingView { HandTrackingView() }
    func updateNSView(_ view: HandTrackingView, context: Context) { view.enabled = enabled }
}

private final class HandTrackingView: NSView {
    var enabled = true {
        didSet { if inside { setCursor() } }
    }
    private var inside = false
    private var tracking: NSTrackingArea?
    override func hitTest(_ point: NSPoint) -> NSView? { nil }
    override func updateTrackingAreas() {
        super.updateTrackingAreas()
        if let tracking { removeTrackingArea(tracking) }
        let area = NSTrackingArea(
            rect: bounds,
            options: [.activeAlways, .inVisibleRect, .mouseEnteredAndExited, .mouseMoved],
            owner: self)
        addTrackingArea(area)
        tracking = area
    }
    private func setCursor() { (enabled ? NSCursor.pointingHand : .arrow).set() }
    override func mouseEntered(with event: NSEvent) {
        inside = true
        setCursor()
    }
    override func mouseMoved(with event: NSEvent) { setCursor() }
    override func mouseExited(with event: NSEvent) {
        inside = false
        NSCursor.arrow.set()
    }
    override func viewWillMove(toWindow newWindow: NSWindow?) {
        if newWindow == nil, inside {
            inside = false
            NSCursor.arrow.set()
        }
        super.viewWillMove(toWindow: newWindow)
    }
}

private struct PointingHand: ViewModifier {
    @Environment(\.isEnabled) private var isEnabled
    let enabled: Bool
    func body(content: Content) -> some View {
        content.background(HandRegion(enabled: enabled && isEnabled))
    }
}

extension View {
    func pointingHand(enabled: Bool = true) -> some View {
        modifier(PointingHand(enabled: enabled))
    }
}

struct PlainHandButton: ButtonStyle {
    func makeBody(configuration: Configuration) -> some View {
        configuration.label.pointingHand().opacity(configuration.isPressed ? 0.7 : 1)
    }
}
