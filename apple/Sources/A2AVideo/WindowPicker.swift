// The system content picker: the person chooses the window whose picture A2A delays.
// The picker grants access to that one window for the session; A2A never enumerates windows.

import Foundation
@preconcurrency import ScreenCaptureKit

public enum WindowPickerError: Error, CustomStringConvertible {
    case canceled
    case failed(String)

    public var description: String {
        switch self {
        case .canceled: "No window was chosen."
        case .failed(let message): "The window picker failed: \(message)"
        }
    }
}

/// The picker hands its filter over once, from its own queue, and nothing mutates it after.
private struct PickedFilter: @unchecked Sendable {
    let filter: SCContentFilter
}

@MainActor public final class WindowPicker: NSObject, SCContentSharingPickerObserver {
    private var continuation: CheckedContinuation<PickedFilter, any Error>?

    public override init() { super.init() }

    /// Shows the system picker limited to single windows (A2A's own excluded) and returns the
    /// chosen window's filter, or throws `.canceled`.
    public func pick() async throws -> SCContentFilter {
        let picker = SCContentSharingPicker.shared
        var configuration = SCContentSharingPickerConfiguration()
        configuration.allowedPickerModes = [.singleWindow]
        if let id = Bundle.main.bundleIdentifier { configuration.excludedBundleIDs = [id] }
        picker.defaultConfiguration = configuration
        picker.add(self)
        picker.isActive = true
        defer { picker.remove(self) }
        let picked = try await withCheckedThrowingContinuation { continuation in
            self.continuation = continuation
            picker.present()
        }
        return picked.filter
    }

    /// Ends the picker's session when the mirror stops.
    public static func deactivate() {
        SCContentSharingPicker.shared.isActive = false
    }

    private func finish(_ result: Result<PickedFilter, any Error>) {
        guard let continuation else { return }
        self.continuation = nil
        continuation.resume(with: result)
    }

    nonisolated public func contentSharingPicker(_ picker: SCContentSharingPicker, didCancelFor stream: SCStream?) {
        Task { @MainActor in self.finish(.failure(WindowPickerError.canceled)) }
    }

    nonisolated public func contentSharingPicker(_ picker: SCContentSharingPicker, didUpdateWith filter: SCContentFilter,
                                                 for stream: SCStream?) {
        let picked = PickedFilter(filter: filter)
        Task { @MainActor in self.finish(.success(picked)) }
    }

    nonisolated public func contentSharingPickerStartDidFailWithError(_ error: any Error) {
        let message = error.localizedDescription
        Task { @MainActor in self.finish(.failure(WindowPickerError.failed(message))) }
    }
}
