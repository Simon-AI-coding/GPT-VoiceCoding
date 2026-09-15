import AppKit
import SwiftUI

/// The Control Panel's one normal window, and the only place that knows its height (ADR 0029).
///
/// Invariant: the content area is as tall as the content's natural height, up to the screen's
/// visible height, and only beyond that does it scroll. The user resizes the width, never the
/// height. Callers decide whether the window is shown and where it anchors; they never measure.
@MainActor
final class ControlWindow: NSObject, NSWindowDelegate {
    let window = NSWindow(
        contentRect: NSRect(x: 0, y: 0, width: Phosphor.windowWidth, height: 1),
        styleMask: [.titled, .closable, .miniaturizable, .resizable, .fullSizeContentView],
        backing: .buffered, defer: true)
    /// Called when the user closes the window from its title bar.
    var onClose: () -> Void = {}
    private var host: NSHostingView<MeasuredContent>!
    private let scroll = NSScrollView()

    init(content: some View) {
        super.init()
        scroll.frame = window.contentView!.bounds
        window.title =
            Bundle.main.object(forInfoDictionaryKey: "CFBundleDisplayName") as? String ?? ""
        window.titleVisibility = .hidden
        window.titlebarAppearsTransparent = true
        window.isReleasedWhenClosed = false
        window.delegate = self
        window.contentMinSize.width = Phosphor.windowWidth
        window.standardWindowButton(.zoomButton)?.isEnabled = false
        let host = NSHostingView(
            rootView: MeasuredContent(content: AnyView(content)) { [weak self] height in
                self?.fit(contentHeight: height)
            })
        host.sizingOptions = [.intrinsicContentSize]
        host.frame.size.width = Phosphor.windowWidth
        host.autoresizingMask = [.width]
        self.host = host
        scroll.drawsBackground = false
        scroll.hasVerticalScroller = true
        scroll.autohidesScrollers = true
        scroll.scrollerStyle = .overlay
        scroll.documentView = host
        window.contentView = scroll
        measure()
    }

    var isVisible: Bool { window.isVisible }

    /// Shows the window at the top of its content. With a lamp frame it hangs below the lamp;
    /// without one, a window that was not already on screen is centred.
    func present(anchor lamp: NSRect?, screen: NSRect?) {
        // A hidden window's content can change without a layout; never open at a stale height.
        measure()
        if !window.isVisible && lamp == nil { window.center() }
        scrollToTop()
        if let lamp, let screen {
            window.setFrame(
                Self.frame(window.frame, height: window.frame.height, anchor: lamp, screen: screen),
                display: true)
        }
        if window.isMiniaturized { window.deminiaturize(nil) }
        window.makeKeyAndOrderFront(nil)
    }

    func dismiss() { window.orderOut(nil) }

    private func measure() {
        host.layoutSubtreeIfNeeded()
        fit(contentHeight: host.fittingSize.height)
    }

    private func fit(contentHeight height: CGFloat) {
        // A zero height before the first layout is not a content measurement.
        guard height > 0 else { return }
        let contentHeight = ceil(height)
        host.frame.size.height = contentHeight
        guard let screen = window.screen ?? NSScreen.main else { return }
        let frame = Self.frame(
            window.frame,
            height: Self.frameHeight(contentHeight: contentHeight, styleMask: window.styleMask),
            anchor: nil, screen: screen.visibleFrame)
        if frame != window.frame { window.setFrame(frame, display: true) }
    }

    private func scrollToTop() {
        guard let document = scroll.documentView else { return }
        let y =
            document.isFlipped
            ? 0 : max(0, document.bounds.height - scroll.contentView.bounds.height)
        scroll.contentView.scroll(to: NSPoint(x: 0, y: y))
        scroll.reflectScrolledClipView(scroll.contentView)
    }

    static func frame(_ current: NSRect, height: CGFloat, anchor: NSRect?, screen: NSRect)
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

    static func frameHeight(contentHeight: CGFloat, styleMask: NSWindow.StyleMask) -> CGFloat {
        let contentRect = NSRect(x: 0, y: 0, width: 1, height: ceil(contentHeight))
        return NSWindow.frameRect(
            forContentRect: contentRect, styleMask: styleMask.subtracting(.fullSizeContentView)
        ).height
    }

    func windowDidResize(_ notification: Notification) {
        let width = scroll.bounds.width
        guard host.rootView.width != width else { return }
        host.rootView.width = width
    }

    func windowWillResize(_ sender: NSWindow, to frameSize: NSSize) -> NSSize {
        NSSize(width: frameSize.width, height: sender.frame.height)
    }

    func windowWillClose(_ notification: Notification) { onClose() }
}

private struct MeasuredContent: View {
    let content: AnyView
    var width = Phosphor.windowWidth
    let changed: (CGFloat) -> Void
    init(content: AnyView, changed: @escaping (CGFloat) -> Void) {
        self.content = content
        self.changed = changed
    }
    var body: some View {
        // Not a preference: in this host it stopped arriving after the first layouts (ADR 0029).
        content.frame(width: width).fixedSize(horizontal: false, vertical: true)
            .onGeometryChange(for: CGFloat.self, of: { $0.size.height }, action: changed)
    }
}
