// Resident spoken-language identifier (ADR-0018): research/lid_worker.py, ECAPA-TDNN VoxLingua107
// on MLX. The pipeline asks it about each closed direct-audio unit so a line already spoken in
// the target language can play the actor instead of a clone. Requests go out on a serial writer,
// never on the caller's thread; answers come back on a reader thread through `onResult`. Only a
// language code and its probability come back; no audio or text is logged.

import Foundation

public final class LanguageIdentifier: @unchecked Sendable {
    public struct Verdict: Sendable, Equatable {
        public let id: Int
        public let language: String
        public let probability: Double
    }

    private let process = Process()
    private let input = Pipe()
    private let output = Pipe()
    private let writer = DispatchQueue(label: "a2a.lid.writer")
    private let lock = NSLock()
    private var readyWaiter: CheckedContinuation<Double, Error>?
    private var readySeconds: Double?
    private var failure: String?
    private var closed = false
    /// Called on the reader thread for each answer; nil language means the request failed.
    public var onResult: (@Sendable (Int, Verdict?) -> Void)?

    public init(python: String, worker: String, model: String) throws {
        process.executableURL = URL(fileURLWithPath: python)
        process.arguments = [worker, "--model", model]
        process.standardInput = input
        process.standardOutput = output
        process.standardError = FileHandle.nullDevice
        try process.run()
        let handle = output.fileHandleForReading
        let thread = Thread { [weak self] in self?.readLoop(handle) }
        thread.name = "a2a.lid.reader"
        thread.start()
    }

    /// Waits for the model to load; returns its load time in seconds.
    public func ready(timeoutSeconds: Double = 60) async throws -> Double {
        try await withThrowingTaskGroup(of: Double.self) { group in
            group.addTask {
                try await withCheckedThrowingContinuation { (c: CheckedContinuation<Double, Error>) in
                    self.lock.lock()
                    if let s = self.readySeconds { self.lock.unlock(); c.resume(returning: s); return }
                    if let f = self.failure { self.lock.unlock(); c.resume(throwing: AdapterError.unsupported(f)); return }
                    self.readyWaiter = c
                    self.lock.unlock()
                }
            }
            group.addTask {
                try await Task.sleep(nanoseconds: UInt64(timeoutSeconds * 1e9))
                throw AdapterError.unsupported("language identifier did not start in \(Int(timeoutSeconds)) s")
            }
            defer { group.cancelAll() }
            return try await group.next()!
        }
    }

    /// Queues one unit of mono 16 kHz audio; the answer arrives through `onResult`.
    public func identify(id: Int, samples16k: [Float]) {
        guard !samples16k.isEmpty else { return }
        let header = try? JSONSerialization.data(withJSONObject: ["id": id, "samples": samples16k.count])
        writer.async { [self] in
            lock.lock(); let isClosed = closed; lock.unlock()
            guard !isClosed, let header else { return }
            var frame = Data()
            var length = UInt32(header.count).littleEndian
            frame.append(Data(bytes: &length, count: 4))
            frame.append(header)
            samples16k.withUnsafeBufferPointer { frame.append(Data(buffer: $0)) }
            do { try input.fileHandleForWriting.write(contentsOf: frame) } catch { fail("language identifier closed") }
        }
    }

    public func shutdown() {
        lock.lock()
        guard !closed else { lock.unlock(); return }
        closed = true
        lock.unlock()
        writer.async { [input, process] in
            try? input.fileHandleForWriting.close()
            if process.isRunning { process.terminate() }
        }
    }

    deinit { shutdown() }

    private func fail(_ message: String) {
        lock.lock()
        failure = failure ?? message
        let waiter = readyWaiter
        readyWaiter = nil
        lock.unlock()
        waiter?.resume(throwing: AdapterError.unsupported(message))
    }

    private func readLoop(_ handle: FileHandle) {
        var buffer = Data()
        while true {
            let chunk = handle.availableData
            if chunk.isEmpty { fail("language identifier exited"); return }
            buffer.append(chunk)
            while let newline = buffer.firstIndex(of: 0x0A) {
                let line = buffer[buffer.startIndex..<newline]
                buffer.removeSubrange(buffer.startIndex...newline)
                guard let event = try? JSONSerialization.jsonObject(with: line) as? [String: Any] else { continue }
                if event["ev"] as? String == "ready" {
                    lock.lock()
                    readySeconds = (event["load_s"] as? NSNumber)?.doubleValue ?? 0
                    let waiter = readyWaiter
                    readyWaiter = nil
                    lock.unlock()
                    waiter?.resume(returning: readySeconds ?? 0)
                } else if let id = (event["id"] as? NSNumber)?.intValue {
                    if let lang = event["lang"] as? String, let p = (event["p"] as? NSNumber)?.doubleValue {
                        onResult?(id, Verdict(id: id, language: lang, probability: p))
                    } else {
                        onResult?(id, nil)
                    }
                }
            }
        }
    }
}
