import AppKit
import Observation
import SwiftUI

final class DutyPanel: NSPanel {
    static let autosaveName = "DutyCard"

    init() {
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
        setFrameAutosaveName(Self.autosaveName)
    }

    override var canBecomeKey: Bool { false }
    override var canBecomeMain: Bool { false }

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
    private var cardPlaced = false
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
            if !cardPlaced {
                if !card.setFrameUsingName(DutyPanel.autosaveName), let screen = NSScreen.main {
                    card.setFrame(
                        DutyPanel.initialFrame(in: screen.visibleFrame, height: card.frame.height),
                        display: false)
                }
                cardPlaced = true
            }
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
