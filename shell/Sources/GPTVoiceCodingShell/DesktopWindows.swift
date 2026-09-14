import AppKit
import Observation
import SwiftUI

final class DutyPanel: NSPanel {
    static let autosaveName = "DutyCard"
    private let savedFrameName: String
    private var placed = false

    init(savedFrameName: String = DutyPanel.autosaveName) {
        self.savedFrameName = savedFrameName
        super.init(
            contentRect: NSRect(x: 0, y: 0, width: Phosphor.cardWidth, height: 30),
            styleMask: [.borderless, .nonactivatingPanel], backing: .buffered, defer: true)
        level = .floating
        collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
        hidesOnDeactivate = false
        isMovableByWindowBackground = true
        isReleasedWhenClosed = false
        isOpaque = false
        backgroundColor = .clear
        hasShadow = true
    }

    override var canBecomeKey: Bool { false }
    override var canBecomeMain: Bool { false }

    func place(in screen: NSRect) {
        guard !placed else { return }
        if !setFrameUsingName(savedFrameName) {
            setFrame(Self.initialFrame(in: screen, height: frame.height), display: false)
        }
        // Hidden hosting-content measurements must not persist a provisional frame.
        setFrameAutosaveName(savedFrameName)
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
    private let card = DutyPanel()
    private let window = NSWindow(
        contentRect: NSRect(x: 0, y: 0, width: Phosphor.windowWidth, height: 1),
        styleMask: [.titled, .closable, .miniaturizable, .resizable, .fullSizeContentView],
        backing: .buffered, defer: true)
    private var lastWindowRequest = 0
    private var localClick: Any?
    private var globalClick: Any?

    init(shell: ShellModel) {
        self.shell = shell
        super.init()
        window.title =
            Bundle.main.object(forInfoDictionaryKey: "CFBundleDisplayName") as? String ?? ""
        window.titleVisibility = .hidden
        window.titlebarAppearsTransparent = true
        window.isReleasedWhenClosed = false
        window.delegate = self
        window.contentMinSize.width = Phosphor.windowWidth
        window.standardWindowButton(.zoomButton)?.isEnabled = false
        window.contentView = NSHostingView(
            rootView:
                MeasuredContent(changed: { [weak self] height in self?.resizeWindow(height: height)
                }) {
                    ControlPanelView(shell: shell)
                })
        card.contentView = NSHostingView(
            rootView:
                MeasuredContent(changed: { [weak self] height in self?.resizeCard(height: height) })
            {
                DutyCardView(shell: shell)
            })
        synchronize()
        observe()
    }

    private func observe() {
        withObservationTracking {
            _ = shell.page
            _ = shell.windowRequest
            _ = shell.cardVisible
        } onChange: { [weak self] in
            Task { @MainActor in
                self?.synchronize()
                self?.observe()
            }
        }
    }

    private func synchronize() {
        if shell.windowOpen {
            if !window.isVisible {
                NSApp.setActivationPolicy(.regular)
                window.center()
            }
            if shell.windowRequest != lastWindowRequest {
                lastWindowRequest = shell.windowRequest
                if window.isMiniaturized { window.deminiaturize(nil) }
                window.makeKeyAndOrderFront(nil)
                NSApp.activate(ignoringOtherApps: true)
            }
        } else {
            window.orderOut(nil)
            NSApp.setActivationPolicy(.accessory)
        }
        if shell.cardVisible {
            if let screen = NSScreen.main { card.place(in: screen.visibleFrame) }
            card.orderFrontRegardless()
            observeOutsideClicks()
        } else {
            card.orderOut(nil)
            shell.cardActionsVisible = false
            stopObservingClicks()
        }
    }

    private func resizeWindow(height: CGFloat) {
        let maximum = (window.screen ?? NSScreen.main)?.visibleFrame.height ?? height
        resize(window, height: min(height, maximum))
    }

    private func resizeCard(height: CGFloat) { resize(card, height: height) }

    private func resize(_ panel: NSWindow, height: CGFloat) {
        var frame = panel.frame
        let height = ceil(height)
        guard frame.height != height else { return }
        frame.origin.y += frame.height - height
        frame.size.height = height
        panel.setFrame(frame, display: true)
    }

    func windowWillResize(_ sender: NSWindow, to frameSize: NSSize) -> NSSize {
        NSSize(width: frameSize.width, height: sender.frame.height)
    }

    func windowWillClose(_ notification: Notification) { shell.closeWindow() }

    private func observeOutsideClicks() {
        guard localClick == nil else { return }
        localClick = NSEvent.addLocalMonitorForEvents(matching: [.leftMouseDown, .rightMouseDown]) {
            [weak self] event in
            if event.window !== self?.card { self?.shell.cardActionsVisible = false }
            return event
        }
        globalClick = NSEvent.addGlobalMonitorForEvents(matching: [.leftMouseDown, .rightMouseDown])
        { [weak self] _ in
            self?.shell.cardActionsVisible = false
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
    let changed: (CGFloat) -> Void
    @ViewBuilder let content: () -> Content
    var body: some View {
        content().fixedSize(horizontal: false, vertical: true)
            .background(
                GeometryReader { geometry in
                    Color.clear.preference(key: ContentHeight.self, value: geometry.size.height)
                }
            )
            .onPreferenceChange(ContentHeight.self, perform: changed)
    }
}
