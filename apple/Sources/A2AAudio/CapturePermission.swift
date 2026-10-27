// System Audio Recording (kTCCServiceAudioCapture) status of this process's responsible app.
//
// No public API reports this before AudioDeviceStart, which blocks inside coreaudiod until
// the user answers the consent prompt. TCCAccessPreflight is private SPI, loaded with dlopen
// so a missing symbol degrades to `.unknown`. Dev and test tools only: review before any
// external release (AGENTS.md).

import Darwin
import Foundation

public enum CapturePermission: String, Sendable {
    case granted
    case denied
    /// The user has not answered yet: starting a tap triggers the prompt.
    case undetermined
    /// The SPI is unavailable on this OS.
    case unknown

    public static func status() -> CapturePermission {
        typealias Preflight = @convention(c) (CFString, CFDictionary?) -> Int32
        guard let handle = dlopen("/System/Library/PrivateFrameworks/TCC.framework/TCC", RTLD_NOW),
              let symbol = dlsym(handle, "TCCAccessPreflight") else { return .unknown }
        let preflight = unsafeBitCast(symbol, to: Preflight.self)
        switch preflight("kTCCServiceAudioCapture" as CFString, nil) {
        case 0: return .granted
        case 1: return .denied
        default: return .undetermined
        }
    }
}
