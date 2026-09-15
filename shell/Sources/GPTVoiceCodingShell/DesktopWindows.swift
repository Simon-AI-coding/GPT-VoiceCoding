import AppKit
import Observation
import SwiftUI

final class DutyPanel: NSPanel {
    static let autosaveName = "DutyCard"
    private let savedFrameName: String
    private var placed = false
    var cancelConfirmation: (() -> Void)?

    init(savedFrameName: String = DutyPanel.autosaveName) {
        self.savedFrameName = savedFrameName
        super.init(
            contentRect: NSRect(x: 0, y: 0, width: Phosphor.cardWidth, height: Phosphor.lampHeight),
            styleMask: [.borderless, .nonactivatingPanel], backing: .buffered, defer: true)
        level = .floating
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
    private let window = NSWindow(
        contentRect: NSRect(x: 0, y: 0, width: Phosphor.windowWidth, height: 1),
        styleMask: [.titled, .closable, .miniaturizable, .resizable, .fullSizeContentView],
        backing: .buffered, defer: true)
    private var contentHost: NSHostingView<MeasuredContent<ControlPanelView>>?
    private weak var contentScroll: NSScrollView?
    private var lastWindowRequest = 0
    private var localClick: Any?
    private var globalClick: Any?

    init(shell: ShellModel, savedFrameName: String = DutyPanel.autosaveName) {
        self.shell = shell
        self.card = DutyPanel(savedFrameName: savedFrameName)
        super.init()
        window.title =
            Bundle.main.object(forInfoDictionaryKey: "CFBundleDisplayName") as? String ?? ""
        window.titleVisibility = .hidden
        window.titlebarAppearsTransparent = true
        window.isReleasedWhenClosed = false
        window.delegate = self
        window.contentMinSize.width = Phosphor.windowWidth
        window.standardWindowButton(.zoomButton)?.isEnabled = false
        let host = NSHostingView(
            rootView:
                MeasuredContent(changed: { [weak self] height in self?.resizeWindow(height: height)
                }) {
                    ControlPanelView(shell: shell)
                })
        host.sizingOptions = [.intrinsicContentSize]
        host.frame.size.width = Phosphor.windowWidth
        host.layoutSubtreeIfNeeded()
        let initialHeight = host.fittingSize.height
        host.autoresizingMask = [.width]
        contentHost = host
        let scroll = NSScrollView(frame: window.contentView!.bounds)
        scroll.drawsBackground = false
        scroll.hasVerticalScroller = true
        scroll.autohidesScrollers = true
        scroll.scrollerStyle = .overlay
        scroll.documentView = host
        window.contentView = scroll
        contentScroll = scroll
        resizeWindow(height: initialHeight)
        card.contentView = LampHostingView(rootView: DutyCardView(shell: shell))
        card.delegate = self
        actions.isMovableByWindowBackground = false
        bubble.isMovableByWindowBackground = false
        card.addChildWindow(actions, ordered: .above)
        card.addChildWindow(bubble, ordered: .above)
        synchronize()
        observe()
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

    private func synchronize() {
        for surface in [window, card, actions, bubble] {
            surface.appearance = shell.selectedAppearance.native
        }

        // Preparation can replace the initial Home page with onboarding after this window exists.
        // Re-read the hosting view's natural height whenever observed shell state changes.
        contentHost?.layoutSubtreeIfNeeded()
        if let height = contentHost?.fittingSize.height { resizeWindow(height: height) }

        if shell.windowOpen {
            if !window.isVisible {
                NSApp.setActivationPolicy(.regular)
                if !shell.windowRequestedFromLamp { window.center() }
            }
            if shell.windowRequest != lastWindowRequest {
                lastWindowRequest = shell.windowRequest
                scrollToTop()
                if shell.windowRequestedFromLamp, shell.cardVisible, let screen = card.screen {
                    window.setFrame(
                        Self.controlFrame(
                            window.frame, height: window.frame.height,
                            anchor: card.frame, screen: screen.visibleFrame), display: true)
                }
                if window.isMiniaturized { window.deminiaturize(nil) }
                window.makeKeyAndOrderFront(nil)
                NSApp.activate(ignoringOtherApps: true)
            }
        } else {
            window.orderOut(nil)
            NSApp.setActivationPolicy(.accessory)
        }
        if shell.cardVisible {
            card.hasShadow = shell.panel.engineReachable
            if let screen = NSScreen.main { card.place(in: screen.visibleFrame) }
            card.orderFrontRegardless()
            synchronizeAttachments()
            observeOutsideClicks()
        } else {
            card.orderOut(nil)
            actions.orderOut(nil)
            bubble.orderOut(nil)
            presentedBubble = nil
            shell.dismissLampActions()
            stopObservingClicks()
        }
    }

    private func resizeWindow(height: CGFloat) {
        // The preference's initial zero is not a content measurement.
        guard height > 0 else { return }
        let contentHeight = ceil(height)
        contentHost?.frame.size.height = contentHeight
        guard let screen = window.screen ?? NSScreen.main else { return }
        let frame = Self.controlFrame(
            window.frame,
            height: Self.controlWindowFrameHeight(
                contentHeight: contentHeight, styleMask: window.styleMask),
            anchor: nil, screen: screen.visibleFrame)
        if frame != window.frame { window.setFrame(frame, display: true) }
    }

    private func scrollToTop() {
        guard let scroll = contentScroll, let document = scroll.documentView else { return }
        let y =
            document.isFlipped
            ? 0 : max(0, document.bounds.height - scroll.contentView.bounds.height)
        scroll.contentView.scroll(to: NSPoint(x: 0, y: y))
        scroll.reflectScrolledClipView(scroll.contentView)
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
        guard notification.object as? NSWindow === card else { return }
        positionAttachments()
    }

    private func updatePointer() {
        let point = NSEvent.mouseLocation
        shell.lampCellHovered =
            card.frame.contains(point)
            && point.x < card.frame.minX + Phosphor.lampEdge + Phosphor.lampCell
        shell.setLampPointer(
            inside: Self.containsReadingPoint(
                NSEvent.mouseLocation,
                lamp: card.frame, bubble: bubble.isVisible ? bubble.frame : nil))
    }

    static func containsReadingPoint(_ point: NSPoint, lamp: NSRect, bubble: NSRect?) -> Bool {
        if lamp.contains(point) { return true }
        guard let bubble else { return false }
        let inBubble = bubble.contains(point)
        let inCrossing =
            NSRect(
                x: bubble.minX,
                y: bubble.maxY, width: bubble.width, height: Phosphor.bubbleGap
            ).contains(point)
        return inBubble || inCrossing
    }

    static func controlFrame(_ current: NSRect, height: CGFloat, anchor: NSRect?, screen: NSRect)
        -> NSRect
    {
        let size = NSSize(
            width: min(current.width, screen.width), height: min(ceil(height), screen.height))
        let right = anchor?.maxX ?? current.maxX
        let top = anchor.map { $0.minY - Phosphor.bubbleGap } ?? current.maxY
        return NSRect(
            x: min(max(right - size.width, screen.minX), screen.maxX - size.width),
            y: min(max(top - size.height, screen.minY), screen.maxY - size.height),
            width: size.width, height: size.height)
    }

    static func controlWindowFrameHeight(
        contentHeight: CGFloat, styleMask: NSWindow.StyleMask
    ) -> CGFloat {
        let contentRect = NSRect(x: 0, y: 0, width: 1, height: ceil(contentHeight))
        return NSWindow.frameRect(
            forContentRect: contentRect, styleMask: styleMask.subtracting(.fullSizeContentView)
        ).height
    }

    func windowDidResize(_ notification: Notification) {
        guard notification.object as? NSWindow === window,
            let width = window.contentView?.bounds.width,
            contentHost?.rootView.width != width
        else { return }
        contentHost?.rootView.width = width
    }

    func windowWillResize(_ sender: NSWindow, to frameSize: NSSize) -> NSSize {
        NSSize(width: frameSize.width, height: sender.frame.height)
    }

    func windowWillClose(_ notification: Notification) { shell.closeWindow() }

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

private struct ContentHeight: PreferenceKey {
    static let defaultValue: CGFloat = 0
    static func reduce(value: inout CGFloat, nextValue: () -> CGFloat) { value = nextValue() }
}

private struct MeasuredContent<Content: View>: View {
    var width = Phosphor.windowWidth
    let changed: (CGFloat) -> Void
    @ViewBuilder let content: () -> Content
    var body: some View {
        content().frame(width: width).fixedSize(horizontal: false, vertical: true)
            .background(
                GeometryReader { geometry in
                    Color.clear.preference(key: ContentHeight.self, value: geometry.size.height)
                }
            )
            .onPreferenceChange(ContentHeight.self, perform: changed)
    }
}

/// Owns the entire plate's hit region, so a text label cannot turn dragging into selection.
@MainActor
private final class LampHostingView: NSHostingView<DutyCardView> {
    private let shell: ShellModel
    private var pointerTracking: NSTrackingArea?
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
    override func mouseEntered(with event: NSEvent) {
        mouseMoved(with: event)
        shell.setLampPointer(inside: true)
    }
    override func mouseMoved(with event: NSEvent) {
        let point = convert(event.locationInWindow, from: nil)
        let inCell = point.x < Phosphor.lampEdge + Phosphor.lampCell
        shell.lampCellHovered = inCell
        (inCell && !shell.lampCellEnabled ? NSCursor.arrow : .pointingHand).set()
    }
    override func mouseExited(with event: NSEvent) {
        shell.lampCellHovered = false
        NSCursor.arrow.set()
    }
    override func rightMouseDown(with event: NSEvent) { shell.toggleLampActions(secondary: true) }
    override func mouseDown(with event: NSEvent) {
        guard let window else { return }
        let inCell =
            convert(event.locationInWindow, from: nil).x < Phosphor.lampEdge + Phosphor.lampCell
        shell.lampCellPressed = inCell && shell.lampCellEnabled
        defer { shell.lampCellPressed = false }
        while let next = window.nextEvent(matching: [.leftMouseDragged, .leftMouseUp]) {
            if next.type == .leftMouseUp {
                if inCell {
                    Task { await shell.activateLampCell() }
                } else {
                    shell.toggleLampActions()
                }
                return
            }
            window.performDrag(with: event)
            return
        }
    }
}

/// A non-intercepting native tracking region; works while its window is inactive.
private struct HandRegion: NSViewRepresentable {
    let enabled: Bool
    func makeNSView(context: Context) -> HandTrackingView { HandTrackingView() }
    func updateNSView(_ view: HandTrackingView, context: Context) { view.enabled = enabled }
}

private final class HandTrackingView: NSView {
    var enabled = true {
        didSet {
            window?.invalidateCursorRects(for: self)
            if inside { setCursor() }
        }
    }
    private var inside = false
    private var tracking: NSTrackingArea?
    override func hitTest(_ point: NSPoint) -> NSView? { nil }
    override func updateTrackingAreas() {
        super.updateTrackingAreas()
        if let tracking { removeTrackingArea(tracking) }
        let area = NSTrackingArea(
            rect: bounds,
            options: [
                .activeAlways, .inVisibleRect, .mouseEnteredAndExited, .mouseMoved, .cursorUpdate,
            ], owner: self)
        addTrackingArea(area)
        tracking = area
    }
    override func resetCursorRects() {
        super.resetCursorRects()
        addCursorRect(bounds, cursor: enabled ? .pointingHand : .arrow)
    }
    private func setCursor() { (enabled ? NSCursor.pointingHand : .arrow).set() }
    override func mouseEntered(with event: NSEvent) {
        inside = true
        setCursor()
    }
    override func mouseMoved(with event: NSEvent) { setCursor() }
    override func cursorUpdate(with event: NSEvent) { setCursor() }
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
