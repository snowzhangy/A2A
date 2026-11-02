// Window showing the delayed picture of the chosen source window (ADR-0012).

import A2AVideo
import AppKit

@MainActor final class PictureWindowController: NSWindowController, NSWindowDelegate {
    private let onClose: () -> Void

    init(mirror: DelayedMirror, sourceSize: CGSize, onClose: @escaping () -> Void) {
        self.onClose = onClose
        let size = Self.initialSize(for: sourceSize)
        let window = NSWindow(contentRect: NSRect(origin: .zero, size: size),
                              styleMask: [.titled, .closable, .miniaturizable, .resizable],
                              backing: .buffered, defer: false)
        window.title = String(format: "A2A picture · %.0f s behind", mirror.delaySeconds)
        window.backgroundColor = .black
        window.collectionBehavior = [.fullScreenPrimary]
        window.isReleasedWhenClosed = false
        if sourceSize.width > 0, sourceSize.height > 0 { window.contentAspectRatio = sourceSize }
        // A layer-hosting view: the mirror's display layer is the view's own layer.
        let view = NSView(frame: NSRect(origin: .zero, size: size))
        view.layer = mirror.layer
        view.wantsLayer = true
        window.contentView = view
        window.setAccessibilityIdentifier("delayed-picture")
        super.init(window: window)
        window.delegate = self
        window.center()
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) { fatalError("not used") }

    /// The source window's size, scaled down to fit two thirds of the main screen.
    private static func initialSize(for source: CGSize) -> CGSize {
        let fallback = CGSize(width: 960, height: 540)
        guard source.width > 0, source.height > 0 else { return fallback }
        let screen = NSScreen.main?.visibleFrame.size ?? CGSize(width: 1440, height: 900)
        let scale = min(1, screen.width * 2 / 3 / source.width, screen.height * 2 / 3 / source.height)
        return CGSize(width: (source.width * scale).rounded(), height: (source.height * scale).rounded())
    }

    func windowWillClose(_ notification: Notification) {
        onClose()
    }
}
