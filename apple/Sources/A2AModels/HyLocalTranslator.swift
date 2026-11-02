// Speech translation by Hy-SimulMT running on this Mac, through the native worker process
// (research/hy_worker/hy_worker.cpp, built by scripts/build_hy_local.sh).
//
// The worker keeps Hy-SimulMT-30B-A3B resident on Metal for the session and translates turn by turn
// in one rolling conversation, so each span hears what came before it (pronouns, names, terms). It
// speaks the same length-prefixed stdin/stdout frames as research/tts_worker.py (see the worker's
// header). Audio travels in memory only and the worker logs timings, never text. Closing its stdin
// ends it, so a host that exits or is killed never leaves the model resident. The model entry
// (`hy-simulmt-30b-a3b-q4km-gguf`) is research-only in models/manifest.yaml.
//
// A span can be streamed while it is spoken (`stream`, then `close`): the worker answers its complete
// turns as the audio arrives, so once the span is committed only its last turn is left. Frames go out
// on a serial writer, never on the caller's thread: the worker reads its input only between turns,
// and a full pipe must not stall the capture loop.

import A2ACore
import Darwin
import Foundation
import Synchronization

public struct HyWorkerInfo: Sendable, Equatable {
    public let model: String
    public let loadSeconds: Double
    public let warmupSeconds: Double
    /// Protocol 2 accepts a span in several appends while it is spoken, and `discard`.
    public let streamsSpans: Bool
}

/// A closed span whose answer the worker is computing; redeem it with `answer(_:unit:)`.
public struct HySpanTicket: Sendable, Hashable {
    let id: Int
    /// Span length at the host rate (bounds the wait, as for a whole span).
    public let samples: Int
}

public final class HyLocalTranslator: @unchecked Sendable {
    /// Rate of the PCM the pipeline hands over; the worker resamples to the model's 16 kHz.
    public static let inputSampleRate = 48_000

    public let direction: String
    public private(set) var info: HyWorkerInfo?
    private let process: Process
    private let stdinPipe = Pipe()
    private let stdoutPipe = Pipe()
    private let inputFD: Int32
    /// Serializes frames and records closure, so no write can reach a reused descriptor.
    private let input = Mutex(false)
    /// Frames leave in call order; a blocked write waits here, not in the caller.
    private let writer = DispatchQueue(label: "a2a.hy-worker.writer", qos: .userInitiated)
    private let shared = Shared()

    private final class Shared: Sendable {
        let state = Mutex(State())
    }

    private typealias Answer = Result<(text: String, stats: TranslationWorkerStats?), Error>

    private struct State {
        var nextID = 0
        var spans: [Int: CheckedContinuation<(text: String, stats: TranslationWorkerStats?), Error>] = [:]
        /// Answers that arrived before anyone asked (a streamed span closes before its request runs).
        var answers: [Int: Answer] = [:]
        var ready: CheckedContinuation<HyWorkerInfo, Error>?
        var readyInfo: HyWorkerInfo?
        var failure: String?

        mutating func deliver(_ id: Int, _ answer: Answer) {
            if let waiter = spans.removeValue(forKey: id) {
                waiter.resume(with: answer)
            } else {
                // Bounded: a span nobody redeems (its unit was dropped) ages out.
                if answers.count >= 32, let oldest = answers.keys.min() { answers[oldest] = nil }
                answers[id] = answer
            }
        }
    }

    /// Starts the worker (`hy-worker --model GGUF`) and waits until the model is loaded and warmed up
    /// (a few seconds when the GGUF is in the page cache).
    public convenience init(worker: URL, model: URL, direction: String, readyTimeout: Duration = .seconds(120)) async throws {
        try await self.init(executable: worker, arguments: ["--model", model.path], direction: direction,
                            readyTimeout: readyTimeout)
    }

    /// Any executable that speaks the worker protocol (tests use research/hy_worker/fake_worker.py).
    public init(executable: URL, arguments: [String], direction: String, readyTimeout: Duration = .seconds(120)) async throws {
        guard ["zh-en", "en-zh"].contains(direction) else {
            throw AdapterError.unsupported("invalid Hy-SimulMT direction")
        }
        self.direction = direction
        inputFD = stdinPipe.fileHandleForWriting.fileDescriptor
        process = Process()
        process.executableURL = executable
        process.arguments = arguments
        process.standardInput = stdinPipe
        process.standardOutput = stdoutPipe
        process.standardError = FileHandle.standardError
        // A dead worker must surface as an error, not SIGPIPE in the host.
        _ = fcntl(inputFD, F_SETNOSIGPIPE, 1)
        try process.run()
        let fd = stdoutPipe.fileHandleForReading.fileDescriptor
        let reader = Thread { [shared] in Self.readLoop(fd: fd, shared: shared) }
        reader.name = "a2a.hy-worker.reader"
        reader.start()
        let timeout = Task { [shared] in
            try await Task.sleep(for: readyTimeout)
            Self.fail(shared, "Hy-SimulMT worker did not become ready within \(readyTimeout)")
        }
        defer { timeout.cancel() }
        do {
            info = try await withCheckedThrowingContinuation { continuation in
                shared.state.withLock { s in
                    if let ready = s.readyInfo { continuation.resume(returning: ready) }
                    else if let failure = s.failure { continuation.resume(throwing: AdapterError.unsupported(failure)) }
                    else { s.ready = continuation }
                }
            }
        } catch {
            shutdown()
            if process.isRunning { process.terminate() }
            throw error
        }
    }

    deinit { shutdown() }

    /// Closes the worker's stdin; it exits on EOF without finishing queued work.
    public func shutdown() {
        input.withLock { closed in
            guard !closed else { return }
            closed = true
            try? stdinPipe.fileHandleForWriting.close()
        }
    }

    /// Set once the worker exits or breaks protocol; every later request fails.
    public var failure: String? { shared.state.withLock { $0.failure } }

    /// Translates one committed span: the worker feeds its turns, answers whatever speech is still
    /// unanswered, and returns everything it committed for the span ("" for music or silence).
    public func translate(unit: UnitID, samples: [Float]) async throws -> TranslationCandidate {
        guard !samples.isEmpty, samples.count <= Self.inputSampleRate * 30 else {
            throw AdapterError.unsupported("Hy-SimulMT segment is empty or exceeds 30 seconds")
        }
        return try await answer(close(remaining: samples, spanSamples: samples.count), unit: unit)
    }

    /// Sends audio of the span being spoken, at `inputSampleRate`. The worker answers each complete
    /// turn as it arrives; the span ends with `close` (or `discard` if it never closes).
    public func stream(_ samples: [Float]) {
        guard !samples.isEmpty else { return }
        send(["op": "append", "id": 0, "direction": direction, "rate": Self.inputSampleRate], payload: samples)
    }

    /// Closes the streamed span with whatever of it was not streamed. The worker answers in order,
    /// so the answer may be ready before it is asked for.
    public func close(remaining samples: [Float] = [], spanSamples: Int) -> HySpanTicket {
        let id = shared.state.withLock { s -> Int in
            s.nextID += 1
            return s.nextID
        }
        if !samples.isEmpty {
            send(["op": "append", "id": id, "direction": direction, "rate": Self.inputSampleRate], payload: samples)
        }
        send(["op": "flush", "id": id])
        return HySpanTicket(id: id, samples: spanSamples)
    }

    /// Forgets a streamed span that will never close (the segmenter dropped it).
    public func discard() {
        send(["op": "discard"])
    }

    /// The answer for a closed span. The wait is bounded as for a whole span sent at once; a late
    /// answer is dropped, and the worker stays usable for the next one.
    public func answer(_ ticket: HySpanTicket, unit: UnitID) async throws -> TranslationCandidate {
        guard ticket.samples > 0, ticket.samples <= Self.inputSampleRate * 30 else {
            throw AdapterError.unsupported("Hy-SimulMT segment is empty or exceeds 30 seconds")
        }
        let timeout = HySimulTranslator.requestTimeout(samples: ticket.samples)
        let id = ticket.id
        let result = try await withThrowingTaskGroup(of: (text: String, stats: TranslationWorkerStats?).self) { group in
            group.addTask { [shared] in
                try await withCheckedThrowingContinuation { continuation in
                    shared.state.withLock { s in
                        if let answer = s.answers.removeValue(forKey: id) { continuation.resume(with: answer) }
                        else if let failure = s.failure { continuation.resume(throwing: AdapterError.unsupported(failure)) }
                        else { s.spans[id] = continuation }
                    }
                }
            }
            group.addTask {
                try await Task.sleep(for: .seconds(timeout))
                throw AdapterError.unsupported("Hy-SimulMT worker did not answer within \(Int(timeout)) s")
            }
            defer { group.cancelAll() }
            do {
                return try await group.next()!
            } catch {
                shared.state.withLock { $0.spans.removeValue(forKey: id) }?.resume(throwing: error)
                throw error
            }
        }
        return TranslationCandidate(unitID: unit, text: result.text, workerStats: result.stats)
    }

    /// Starts a new conversation (for example after a seek, or a change of programme).
    public func reset() {
        send(["op": "reset"])
    }

    // MARK: Protocol

    private func send(_ header: [String: Any], payload: [Float] = []) {
        guard let json = try? JSONSerialization.data(withJSONObject: header) else { return }
        var frame = Data(capacity: 8 + json.count + payload.count * 4)
        withUnsafeBytes(of: UInt32(json.count).littleEndian) { frame.append(contentsOf: $0) }
        withUnsafeBytes(of: UInt32(payload.count * 4).littleEndian) { frame.append(contentsOf: $0) }
        frame.append(json)
        payload.withUnsafeBytes { frame.append(contentsOf: $0) }
        let fd = inputFD
        writer.async { [self, frame] in
            let ok = input.withLock { closed in
                guard !closed else { return false }
                return frame.withUnsafeBytes { raw -> Bool in
                    var offset = 0
                    while offset < raw.count {
                        let n = write(fd, raw.baseAddress! + offset, raw.count - offset)
                        if n < 0, errno == EINTR { continue }
                        if n <= 0 { return false }
                        offset += n
                    }
                    return true
                }
            }
            if !ok { Self.fail(shared, "Hy-SimulMT worker input closed") }
        }
    }

    private static func readLoop(fd: Int32, shared: Shared) {
        func readExact(_ count: Int) -> [UInt8]? {
            var buffer = [UInt8](repeating: 0, count: count)
            var offset = 0
            while offset < count {
                let n = buffer.withUnsafeMutableBytes { read(fd, $0.baseAddress! + offset, count - offset) }
                if n < 0, errno == EINTR { continue }
                if n <= 0 { return nil }
                offset += n
            }
            return buffer
        }
        while true {
            guard let prefix = readExact(8) else { break }
            let headerLength = Int(prefix[0..<4].withUnsafeBytes { $0.loadUnaligned(as: UInt32.self).littleEndian })
            let payloadLength = Int(prefix[4..<8].withUnsafeBytes { $0.loadUnaligned(as: UInt32.self).littleEndian })
            guard headerLength <= 1 << 20, payloadLength <= 1 << 20,
                  let header = readExact(headerLength), payloadLength == 0 || readExact(payloadLength) != nil else {
                fail(shared, "Hy-SimulMT worker protocol error")
                return
            }
            // Foundation objects from JSON parsing: drain per frame on this long-lived thread.
            let ok = autoreleasepool { handle(header: header, shared: shared) }
            if !ok {
                fail(shared, "Hy-SimulMT worker protocol error")
                return
            }
        }
        fail(shared, "Hy-SimulMT worker exited")
    }

    private static func handle(header: [UInt8], shared: Shared) -> Bool {
        guard let event = try? JSONSerialization.jsonObject(with: Data(header)) as? [String: Any],
              let kind = event["ev"] as? String else { return false }
        let id = (event["id"] as? NSNumber)?.intValue
        switch kind {
        case "ready":
            let number = { (key: String) in (event[key] as? NSNumber)?.doubleValue ?? 0 }
            let info = HyWorkerInfo(model: event["model"] as? String ?? "", loadSeconds: number("load_s"),
                                    warmupSeconds: number("warmup_s"), streamsSpans: number("protocol") >= 2)
            let waiter = shared.state.withLock { s -> CheckedContinuation<HyWorkerInfo, Error>? in
                s.readyInfo = info
                defer { s.ready = nil }
                return s.ready
            }
            waiter?.resume(returning: info)
        case "delta":
            break  // span mode answers with the joined text in "done"
        case "done":
            guard let id else { return false }
            let text = event["text"] as? String ?? ""
            let stats = (event["flush_ms"] as? NSNumber).map {
                TranslationWorkerStats(afterCloseMs: $0.intValue, turns: (event["turns"] as? NSNumber)?.intValue ?? 0,
                                       sentenceFallback: event["st"] as? Bool ?? false)
            }
            shared.state.withLock { $0.deliver(id, .success((text, stats))) }
        case "error":
            let message = event["message"] as? String ?? "Hy-SimulMT worker error"
            if let id {
                // Streamed audio carries id 0: nobody waits for it; its span's flush still answers.
                if id != 0 { shared.state.withLock { $0.deliver(id, .failure(AdapterError.unsupported(message))) } }
            } else {
                fail(shared, message)
            }
        default:
            return false
        }
        return true
    }

    /// Fails everything waiting on the worker and every later request.
    private static func fail(_ shared: Shared, _ message: String) {
        let (ready, spans) = shared.state.withLock { s in
            if s.failure == nil { s.failure = message }
            defer { s.ready = nil; s.spans.removeAll(); s.answers.removeAll() }
            return (s.ready, Array(s.spans.values))
        }
        let error = AdapterError.unsupported(message)
        ready?.resume(throwing: error)
        spans.forEach { $0.resume(throwing: error) }
    }
}
