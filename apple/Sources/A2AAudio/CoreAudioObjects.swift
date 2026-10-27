// Thin, typed access to Core Audio HAL objects: devices, processes, taps.
// Control-thread only.

import CoreAudio
import Darwin
import Foundation

public struct CoreAudioError: Error, CustomStringConvertible {
    public let status: OSStatus
    public let what: String

    public init(_ status: OSStatus, _ what: String) {
        self.status = status
        self.what = what
    }

    public var description: String {
        let code = UInt32(bitPattern: status)
        let chars = [24, 16, 8, 0].map { UInt8((code >> $0) & 0xFF) }
        let fourCC = chars.allSatisfy { $0 >= 32 && $0 < 127 } ? " '\(String(decoding: chars, as: UTF8.self))'" : ""
        return "\(what) failed: OSStatus \(status)\(fourCC)"
    }
}

@inline(__always)
func check(_ status: OSStatus, _ what: @autoclosure () -> String) throws {
    if status != noErr { throw CoreAudioError(status, what()) }
}

enum HAL {
    static func address(_ selector: AudioObjectPropertySelector,
                        _ scope: AudioObjectPropertyScope = kAudioObjectPropertyScopeGlobal,
                        _ element: AudioObjectPropertyElement = kAudioObjectPropertyElementMain) -> AudioObjectPropertyAddress {
        AudioObjectPropertyAddress(mSelector: selector, mScope: scope, mElement: element)
    }

    static func has(_ object: AudioObjectID, _ selector: AudioObjectPropertySelector,
                    scope: AudioObjectPropertyScope = kAudioObjectPropertyScopeGlobal) -> Bool {
        var addr = address(selector, scope)
        return AudioObjectHasProperty(object, &addr)
    }

    static func scalar<T: BitwiseCopyable>(_ object: AudioObjectID, _ selector: AudioObjectPropertySelector,
                          scope: AudioObjectPropertyScope = kAudioObjectPropertyScopeGlobal,
                          qualifier: UnsafeRawPointer? = nil, qualifierSize: UInt32 = 0,
                          default value: T) throws -> T {
        var addr = address(selector, scope)
        var out = value
        var size = UInt32(MemoryLayout<T>.size)
        try check(AudioObjectGetPropertyData(object, &addr, qualifierSize, qualifier, &size, &out),
                  "get \(fourCC(selector)) on \(object)")
        return out
    }

    static func array<T: BitwiseCopyable>(_ object: AudioObjectID, _ selector: AudioObjectPropertySelector,
                         scope: AudioObjectPropertyScope = kAudioObjectPropertyScopeGlobal, element: T) throws -> [T] {
        var addr = address(selector, scope)
        var size: UInt32 = 0
        try check(AudioObjectGetPropertyDataSize(object, &addr, 0, nil, &size), "size \(fourCC(selector))")
        let count = Int(size) / MemoryLayout<T>.stride
        guard count > 0 else { return [] }
        var out = [T](repeating: element, count: count)
        try check(AudioObjectGetPropertyData(object, &addr, 0, nil, &size, &out), "get \(fourCC(selector))")
        return Array(out.prefix(Int(size) / MemoryLayout<T>.stride))
    }

    static func string(_ object: AudioObjectID, _ selector: AudioObjectPropertySelector,
                       scope: AudioObjectPropertyScope = kAudioObjectPropertyScopeGlobal) throws -> String {
        var addr = address(selector, scope)
        var size = UInt32(MemoryLayout<CFString?>.size)
        var value: Unmanaged<CFString>?
        try check(withUnsafeMutablePointer(to: &value) {
            AudioObjectGetPropertyData(object, &addr, 0, nil, &size, $0)
        }, "get \(fourCC(selector))")
        return value?.takeRetainedValue() as String? ?? ""
    }

    static func fourCC(_ v: UInt32) -> String {
        String(decoding: [24, 16, 8, 0].map { UInt8((v >> $0) & 0xFF) }, as: UTF8.self)
    }
}

// MARK: Devices

public struct AudioDevice: Sendable, CustomStringConvertible {
    public let id: AudioObjectID

    public init(id: AudioObjectID) { self.id = id }

    public static func defaultOutput() throws -> AudioDevice {
        let id: AudioObjectID = try HAL.scalar(AudioObjectID(kAudioObjectSystemObject),
                                               kAudioHardwarePropertyDefaultOutputDevice, default: 0)
        guard id != kAudioObjectUnknown else { throw CoreAudioError(-1, "no default output device") }
        return AudioDevice(id: id)
    }

    public static func all() throws -> [AudioDevice] {
        try HAL.array(AudioObjectID(kAudioObjectSystemObject), kAudioHardwarePropertyDevices, element: AudioObjectID(0))
            .map(AudioDevice.init)
    }

    public var uid: String { (try? HAL.string(id, kAudioDevicePropertyDeviceUID)) ?? "" }
    public var name: String { (try? HAL.string(id, kAudioObjectPropertyName)) ?? "" }
    public var nominalSampleRate: Double {
        (try? HAL.scalar(id, kAudioDevicePropertyNominalSampleRate, default: Float64(0))) ?? 0
    }
    public var bufferFrameSize: UInt32 { (try? HAL.scalar(id, kAudioDevicePropertyBufferFrameSize, default: UInt32(0))) ?? 0 }
    public func latencyFrames(scope: AudioObjectPropertyScope) -> UInt32 {
        (try? HAL.scalar(id, kAudioDevicePropertyLatency, scope: scope, default: UInt32(0))) ?? 0
    }
    public func safetyOffsetFrames(scope: AudioObjectPropertyScope) -> UInt32 {
        (try? HAL.scalar(id, kAudioDevicePropertySafetyOffset, scope: scope, default: UInt32(0))) ?? 0
    }
    public var transportType: UInt32 { (try? HAL.scalar(id, kAudioDevicePropertyTransportType, default: UInt32(0))) ?? 0 }
    public var transportTypeName: String {
        switch transportType {
        case kAudioDeviceTransportTypeBuiltIn: "built-in"
        case kAudioDeviceTransportTypeBluetooth, kAudioDeviceTransportTypeBluetoothLE: "bluetooth"
        case kAudioDeviceTransportTypeUSB: "usb"
        case kAudioDeviceTransportTypeAggregate: "aggregate"
        case kAudioDeviceTransportTypeVirtual: "virtual"
        case kAudioDeviceTransportTypeHDMI, kAudioDeviceTransportTypeDisplayPort: "display"
        default: HAL.fourCC(transportType)
        }
    }

    public func channelCount(scope: AudioObjectPropertyScope) -> Int {
        var addr = HAL.address(kAudioDevicePropertyStreamConfiguration, scope)
        var size: UInt32 = 0
        guard AudioObjectGetPropertyDataSize(id, &addr, 0, nil, &size) == noErr, size > 0 else { return 0 }
        let raw = UnsafeMutableRawPointer.allocate(byteCount: Int(size), alignment: 16)
        defer { raw.deallocate() }
        guard AudioObjectGetPropertyData(id, &addr, 0, nil, &size, raw) == noErr else { return 0 }
        let abl = UnsafeMutableAudioBufferListPointer(raw.assumingMemoryBound(to: AudioBufferList.self))
        return abl.reduce(0) { $0 + Int($1.mNumberChannels) }
    }

    /// Physical formats of the device's streams in `scope`.
    public func streamFormats(scope: AudioObjectPropertyScope) -> [AudioStreamBasicDescription] {
        guard let streams = try? HAL.array(id, kAudioDevicePropertyStreams, scope: scope, element: AudioObjectID(0)) else {
            return []
        }
        return streams.compactMap {
            try? HAL.scalar($0, kAudioStreamPropertyVirtualFormat, default: AudioStreamBasicDescription())
        }
    }

    public var description: String { "\(name) [\(uid)] \(Int(nominalSampleRate)) Hz \(transportTypeName)" }
}

// MARK: Processes

public struct AudioProcessInfo: Sendable, Hashable, CustomStringConvertible {
    public let objectID: AudioObjectID
    public let pid: pid_t
    public let bundleID: String
    public let isRunning: Bool
    public let isRunningOutput: Bool
    public let isRunningInput: Bool
    public let executablePath: String
    public let parentPID: pid_t

    public var description: String {
        "\(objectID) pid=\(pid) ppid=\(parentPID) \(bundleID.isEmpty ? "-" : bundleID) out=\(isRunningOutput) in=\(isRunningInput) \(executablePath)"
    }

    /// Root `.app` bundle path containing the executable, if any (outermost bundle).
    public var appBundleRoot: String? {
        guard let r = executablePath.range(of: ".app/") else { return nil }
        return String(executablePath[..<r.lowerBound]) + ".app"
    }
}

public enum AudioProcessList {
    public static func current() throws -> [AudioProcessInfo] {
        let ids = try HAL.array(AudioObjectID(kAudioObjectSystemObject), kAudioHardwarePropertyProcessObjectList,
                                element: AudioObjectID(0))
        return ids.compactMap(info)
    }

    public static func info(_ id: AudioObjectID) -> AudioProcessInfo? {
        guard let pid: pid_t = try? HAL.scalar(id, kAudioProcessPropertyPID, default: pid_t(-1)), pid > 0 else { return nil }
        func flag(_ s: AudioObjectPropertySelector) -> Bool {
            ((try? HAL.scalar(id, s, default: UInt32(0))) ?? 0) != 0
        }
        return AudioProcessInfo(
            objectID: id, pid: pid,
            bundleID: (try? HAL.string(id, kAudioProcessPropertyBundleID)) ?? "",
            isRunning: flag(kAudioProcessPropertyIsRunning),
            isRunningOutput: flag(kAudioProcessPropertyIsRunningOutput),
            isRunningInput: flag(kAudioProcessPropertyIsRunningInput),
            executablePath: executablePath(pid), parentPID: parentPID(pid))
    }

    public static func objectID(forPID pid: pid_t) throws -> AudioObjectID {
        var p = pid
        let id: AudioObjectID = try withUnsafePointer(to: &p) {
            try HAL.scalar(AudioObjectID(kAudioObjectSystemObject), kAudioHardwarePropertyTranslatePIDToProcessObject,
                           qualifier: $0, qualifierSize: UInt32(MemoryLayout<pid_t>.size), default: AudioObjectID(0))
        }
        guard id != kAudioObjectUnknown else { throw CoreAudioError(-1, "pid \(pid) has no audio process object") }
        return id
    }

    static func executablePath(_ pid: pid_t) -> String {
        var buf = [UInt8](repeating: 0, count: 4 * Int(MAXPATHLEN))
        let n = proc_pidpath(pid, &buf, UInt32(buf.count))
        return n > 0 ? String(decoding: buf.prefix(Int(n)), as: UTF8.self) : ""
    }

    static func parentPID(_ pid: pid_t) -> pid_t {
        var info = proc_bsdinfo()
        let n = proc_pidinfo(pid, PROC_PIDTBSDINFO, 0, &info, Int32(MemoryLayout<proc_bsdinfo>.size))
        return n > 0 ? pid_t(info.pbi_ppid) : 0
    }
}

/// Live process tap objects (used to verify teardown after a host crash).
public enum TapList {
    public static func current() -> [AudioObjectID] {
        (try? HAL.array(AudioObjectID(kAudioObjectSystemObject), kAudioHardwarePropertyTapList,
                        element: AudioObjectID(0))) ?? []
    }

    public static func uid(_ tap: AudioObjectID) -> String {
        (try? HAL.string(tap, kAudioTapPropertyUID)) ?? ""
    }

    /// Destroys a public tap by UID from any process: cleanup for taps a dead test host left
    /// behind. Returns nil when no live tap has that UID.
    public static func destroy(uid: String) -> OSStatus? {
        guard let id = current().first(where: { Self.uid($0).uppercased() == uid.uppercased() }) else { return nil }
        return AudioHardwareDestroyProcessTap(id)
    }
}
