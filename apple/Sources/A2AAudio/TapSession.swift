// Process tap + private aggregate device + RT engine (A1/A2, ADR-0002).
//
// The aggregate contains the tap and the current default output device, so one IOProc
// sees tapped input and device output on the same clock. The IOProc is the C function
// a2a_engine_ioproc: no Swift, ARC, or locks on the real-time thread.

import A2ARTShim
import CoreAudio
import Foundation

public enum TapMute: String, Sendable, CaseIterable {
    case unmuted
    case muted
    case mutedWhenTapped = "muted-when-tapped"

    var behavior: CATapMuteBehavior {
        switch self {
        case .unmuted: .unmuted
        case .muted: .muted
        case .mutedWhenTapped: .mutedWhenTapped
        }
    }
}

public struct TapSessionOptions: Sendable {
    public var mute: TapMute = .unmuted
    /// Re-render captured audio through our mixer (A2). Off = capture only.
    public var renderOutput = false
    public var delayMs = 0
    public var maxDelayMs = 3_000
    public var captureRingSeconds = 4.0
    public var translatedRingSeconds = 30.0
    /// Post-gain monitor ring for diagnostics; 0 allocates none.
    public var renderedRingSeconds = 0.0
    public var name = "A2A Tap"
    /// Private taps are visible only to the creating process. Lifecycle tests make the
    /// child's tap public so the supervising process can observe its teardown.
    public var privateTap = true

    public init() {}
}

public final class TapSession: @unchecked Sendable {
    public let source: ResolvedSource
    public let options: TapSessionOptions
    public let outputDevice: AudioDevice
    public private(set) var tapID = AudioObjectID(kAudioObjectUnknown)
    public private(set) var tapUUID = UUID()
    public private(set) var tapFormat = AudioStreamBasicDescription()
    public private(set) var aggregateID = AudioObjectID(kAudioObjectUnknown)
    public private(set) var sampleRate: Double = 0
    public private(set) var inputChannelOffset = 0
    public let channels: UInt32 = 2
    /// Translated-bus fades at flushes, dry spells, and resumption: short enough to keep
    /// fallbacks prompt, long enough that no edge clicks.
    public static let edgeFadeSeconds = 0.005
    /// The RT engine the IOProc runs. Drivers use its control and counter API
    /// (`a2a_engine_*`); never call `a2a_engine_process` on it while the device runs.
    public private(set) var engine: OpaquePointer?
    private var procID: AudioDeviceIOProcID?
    private var running = false

    public init(source: ResolvedSource, options: TapSessionOptions = .init()) throws {
        self.source = source
        self.options = options
        self.outputDevice = try AudioDevice.defaultOutput()
        do {
            try createTap()
            try createAggregate()
            try createEngine()
        } catch {
            teardown()
            throw error
        }
    }

    deinit { teardown() }

    public var tapIsMuted: Bool { options.mute != .unmuted }
    public var captureRing: OpaquePointer? { engine.map { a2a_engine_capture_ring($0) } }
    public var translatedRing: OpaquePointer? { engine.map { a2a_engine_translated_ring($0) } }
    public var recordRing: OpaquePointer? { engine.map { a2a_engine_records($0) } }

    private func createTap() throws {
        let description = CATapDescription(stereoMixdownOfProcesses: source.objectIDs)
        description.uuid = tapUUID
        description.name = options.name
        description.isPrivate = options.privateTap
        description.muteBehavior = options.mute.behavior
        var id = AudioObjectID(kAudioObjectUnknown)
        try check(AudioHardwareCreateProcessTap(description, &id), "AudioHardwareCreateProcessTap")
        tapID = id
        tapFormat = try HAL.scalar(id, kAudioTapPropertyFormat, default: AudioStreamBasicDescription())
    }

    private func createAggregate() throws {
        let outputUID = outputDevice.uid
        let description: [String: Any] = [
            kAudioAggregateDeviceNameKey: options.name,
            kAudioAggregateDeviceUIDKey: "dev.a2a.aggregate.\(UUID().uuidString)",
            kAudioAggregateDeviceMainSubDeviceKey: outputUID,
            kAudioAggregateDeviceIsPrivateKey: true,
            kAudioAggregateDeviceIsStackedKey: false,
            kAudioAggregateDeviceTapAutoStartKey: true,
            kAudioAggregateDeviceSubDeviceListKey: [[kAudioSubDeviceUIDKey: outputUID]],
            kAudioAggregateDeviceTapListKey: [[kAudioSubTapDriftCompensationKey: true,
                                               kAudioSubTapUIDKey: tapUUID.uuidString]],
        ]
        var id = AudioObjectID(kAudioObjectUnknown)
        try check(AudioHardwareCreateAggregateDevice(description as CFDictionary, &id), "AudioHardwareCreateAggregateDevice")
        aggregateID = id
        let aggregate = AudioDevice(id: id)
        sampleRate = aggregate.nominalSampleRate
        // Sub-device inputs precede tap channels in the aggregate's input streams.
        let totalInputs = aggregate.channelCount(scope: kAudioObjectPropertyScopeInput)
        inputChannelOffset = max(0, totalInputs - Int(tapFormat.mChannelsPerFrame))
        for format in aggregate.streamFormats(scope: kAudioObjectPropertyScopeInput) + aggregate.streamFormats(scope: kAudioObjectPropertyScopeOutput) {
            guard format.mFormatID == kAudioFormatLinearPCM, format.mFormatFlags & kAudioFormatFlagIsFloat != 0,
                  format.mBitsPerChannel == 32 else {
                throw CoreAudioError(-1, "aggregate stream is not Float32 linear PCM (\(format))")
            }
        }
    }

    private func createEngine() throws {
        let rate = sampleRate > 0 ? sampleRate : 48_000
        var cfg = a2a_engine_config(
            channels: channels,
            capture_ring_frames: UInt32(rate * options.captureRingSeconds),
            max_delay_frames: UInt32(rate * Double(options.maxDelayMs) / 1_000),
            translated_ring_frames: UInt32(rate * options.translatedRingSeconds),
            max_block_frames: 8_192,
            record_capacity: 16_384,
            rendered_ring_frames: UInt32(rate * options.renderedRingSeconds),
            fade_frames: UInt32(rate * Self.edgeFadeSeconds))
        guard let e = a2a_engine_create(&cfg) else { throw CoreAudioError(-1, "a2a_engine_create") }
        engine = e
        a2a_engine_set_input_channel_offset(e, UInt32(inputChannelOffset))
        a2a_engine_set_render_output(e, options.renderOutput)
        setDelay(ms: options.delayMs)
        var proc: AudioDeviceIOProcID?
        try check(AudioDeviceCreateIOProcID(aggregateID, a2a_engine_ioproc, UnsafeMutableRawPointer(e), &proc),
                  "AudioDeviceCreateIOProcID")
        procID = proc
    }

    public func start() throws {
        guard !running, let procID else { return }
        try check(AudioDeviceStart(aggregateID, procID), "AudioDeviceStart")
        running = true
    }

    public func stop() {
        guard running, let procID else { return }
        AudioDeviceStop(aggregateID, procID)
        running = false
    }

    /// Stop the device and destroy the IOProc, aggregate, and tap (restores the source's sound).
    public func teardown() {
        stop()
        if let procID, aggregateID != kAudioObjectUnknown {
            AudioDeviceDestroyIOProcID(aggregateID, procID)
        }
        procID = nil
        if aggregateID != kAudioObjectUnknown {
            AudioHardwareDestroyAggregateDevice(aggregateID)
            aggregateID = AudioObjectID(kAudioObjectUnknown)
        }
        if tapID != kAudioObjectUnknown {
            AudioHardwareDestroyProcessTap(tapID)
            tapID = AudioObjectID(kAudioObjectUnknown)
        }
        if let engine {
            a2a_engine_destroy(engine)
            self.engine = nil
        }
    }

    // MARK: Control-thread API

    public func setDelay(ms: Int) {
        guard let engine else { return }
        a2a_engine_set_delay_frames(engine, UInt32(max(0, Double(ms)) * max(sampleRate, 1) / 1_000))
    }

    public func setGains(original: Float, translated: Float, rampMs: Int) {
        guard let engine else { return }
        a2a_engine_set_gains(engine, original, translated, UInt32(Double(rampMs) * max(sampleRate, 1) / 1_000))
    }

    public func setRenderOutput(_ enabled: Bool) {
        guard let engine else { return }
        a2a_engine_set_render_output(engine, enabled)
    }

    /// Reads interleaved captured frames (engine channels). Returns frames read.
    public func readCapture(into buffer: inout [Float]) -> Int {
        guard let ring = captureRing else { return 0 }
        let maxFrames = UInt32(buffer.count / Int(channels))
        return buffer.withUnsafeMutableBufferPointer { Int(a2a_ring_read(ring, $0.baseAddress!, maxFrames)) }
    }

    /// Queues mono translated PCM at the device rate. Returns frames accepted.
    @discardableResult
    public func enqueueTranslated(_ mono: [Float]) -> Int {
        guard let ring = translatedRing else { return 0 }
        return mono.withUnsafeBufferPointer {
            $0.isEmpty ? 0 : Int(a2a_ring_write(ring, $0.baseAddress!, UInt32($0.count)))
        }
    }

    public func flushTranslated() {
        guard let engine else { return }
        a2a_engine_flush_translated(engine)
    }

    public struct Counters: Sendable {
        public let callbacks: UInt64
        public let capturedFrames: UInt64
        public let outputFrames: UInt64
        public let captureOverflowFrames: UInt64
        public let translatedStarvedFrames: UInt64
        public let rejectedBlocks: UInt64
        public let recordDrops: UInt64
    }

    public var counters: Counters {
        guard let engine else { return Counters(callbacks: 0, capturedFrames: 0, outputFrames: 0, captureOverflowFrames: 0,
                                                translatedStarvedFrames: 0, rejectedBlocks: 0, recordDrops: 0) }
        return Counters(callbacks: a2a_engine_callbacks(engine), capturedFrames: a2a_engine_captured_frames(engine),
                        outputFrames: a2a_engine_output_frames(engine),
                        captureOverflowFrames: a2a_ring_overflow_frames(a2a_engine_capture_ring(engine)),
                        translatedStarvedFrames: a2a_engine_translated_starved_frames(engine),
                        rejectedBlocks: a2a_engine_rejected_blocks(engine),
                        recordDrops: a2a_record_ring_dropped(a2a_engine_records(engine)))
    }

    /// Device-side latency metadata for the uncertainty report (section 4.1).
    public var latencyReport: [String: Double] {
        let rate = max(sampleRate, 1)
        let out = outputDevice
        return [
            "sample_rate": sampleRate,
            "output_latency_ms": Double(out.latencyFrames(scope: kAudioObjectPropertyScopeOutput)) / rate * 1_000,
            "output_safety_offset_ms": Double(out.safetyOffsetFrames(scope: kAudioObjectPropertyScopeOutput)) / rate * 1_000,
            "output_buffer_ms": Double(out.bufferFrameSize) / rate * 1_000,
            "aggregate_input_latency_ms": Double(AudioDevice(id: aggregateID).latencyFrames(scope: kAudioObjectPropertyScopeInput)) / rate * 1_000,
            "aggregate_input_safety_offset_ms": Double(AudioDevice(id: aggregateID).safetyOffsetFrames(scope: kAudioObjectPropertyScopeInput)) / rate * 1_000,
            "tap_sample_rate": tapFormat.mSampleRate,
            "tap_channels": Double(tapFormat.mChannelsPerFrame),
        ]
    }
}
