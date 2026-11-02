// Resident local research MT. This adapter performs no audio I/O or runtime downloads.
import A2ACore
import Foundation
import Synchronization
import Darwin

public final class ContextualTranslator: IncrementalTranslator, @unchecked Sendable {
    public var supportsContext: Bool { true }
    public var supportsPrefix: Bool { false }
    private struct State {
        var ready = false
        var stopped = false
        var startup: CheckedContinuation<Void, Error>?
        var requests: [Int: CheckedContinuation<String, Error>] = [:]
    }
    private final class Shared: @unchecked Sendable { let value = Mutex(State()) }
    private let state = Shared()
    private let sendLock = NSLock()
    private let process = Process()
    private let input = Pipe()
    private let output = Pipe()
    var pendingRequestCount: Int { state.value.withLock { $0.requests.count } }

    public init(python: URL, worker: URL, arguments: [String] = []) throws {
        process.executableURL = python
        process.arguments = [worker.path] + arguments
        process.standardInput = input
        process.standardOutput = output
        process.standardError = FileHandle.standardError
        guard fcntl(input.fileHandleForWriting.fileDescriptor, F_SETNOSIGPIPE, 1) == 0 else {
            throw AdapterError.unsupported("cannot configure context worker pipe")
        }
        try process.run()
        let state = self.state, handle = output.fileHandleForReading
        Task.detached {
            try? await Task.sleep(for: .seconds(120))
            if state.value.withLock({ !$0.ready && !$0.stopped }) { Self.failAll(state) }
        }
        Thread.detachNewThread {
            var pending = Data()
            while true {
                let data = handle.availableData
                if data.isEmpty { break }
                pending.append(data)
                // Python bounds output; the host independently enforces framing limits.
                if pending.count > 131_072 { break }
                while let newline = pending.firstIndex(of: 10) {
                    let line = pending.prefix(upTo: newline)
                    guard let event = try? JSONSerialization.jsonObject(with: line) as? [String: Any],
                          let kind = event["ev"] as? String else {
                        Self.failAll(state); return
                    }
                    pending.removeSubrange(...newline)
                    switch kind {
                    case "ready":
                        let continuation = state.value.withLock { s in
                            s.ready = true
                            defer { s.startup = nil }
                            return s.startup
                        }
                        continuation?.resume()
                    case "result", "error":
                        guard let id = event["id"] as? Int else { Self.failAll(state); return }
                        let continuation = state.value.withLock { $0.requests.removeValue(forKey: id) }
                        if kind == "result", let text = event["text"] as? String {
                            continuation?.resume(returning: text)
                        } else {
                            continuation?.resume(throwing: AdapterError.unsupported("context translation failed"))
                        }
                    default: Self.failAll(state); return
                    }
                }
            }
            Self.failAll(state)
        }
    }

    private static func failAll(_ state: Shared) {
        let (startup, requests) = state.value.withLock { s in
            s.stopped = true
            defer { s.startup = nil; s.requests.removeAll() }
            return (s.startup, Array(s.requests.values))
        }
        startup?.resume(throwing: AdapterError.unsupported("context worker stopped"))
        requests.forEach { $0.resume(throwing: AdapterError.unsupported("context worker stopped")) }
    }

    public func waitUntilReady() async throws {
        try await withCheckedThrowingContinuation { (continuation: CheckedContinuation<Void, Error>) in
            state.value.withLock { s in
                if s.stopped { continuation.resume(throwing: AdapterError.unsupported("context worker stopped")) }
                else if s.ready { continuation.resume() }
                else if s.startup != nil { continuation.resume(throwing: AdapterError.unsupported("startup already pending")) }
                else { s.startup = continuation }
            }
        }
    }

    private func send(_ message: [String: Any]) throws {
        var data = try JSONSerialization.data(withJSONObject: message)
        guard data.count < 65536 else { throw AdapterError.unsupported("context request exceeds 64KB") }
        data.append(10)
        sendLock.lock()
        defer { sendLock.unlock() }
        try input.fileHandleForWriting.write(contentsOf: data)
    }

    public func translate(_ req: TranslationRequest) async throws -> TranslationCandidate {
        let id = req.unitID.rawValue
        let text = try await withTaskCancellationHandler {
            try Task.checkCancellation()
            return try await withCheckedThrowingContinuation { (continuation: CheckedContinuation<String, Error>) in
                let registered = state.value.withLock { s -> Bool in
                    guard s.ready, !s.stopped, s.requests[id] == nil, s.requests.count < 16 else { return false }
                    s.requests[id] = continuation
                    return true
                }
                guard registered else {
                    continuation.resume(throwing: AdapterError.unsupported("context worker unavailable or queue full"))
                    return
                }
                do {
                    try send(["op": "translate", "id": id, "source": req.uncommittedSource,
                              "context": req.context.suffix(8).enumerated().map { i, p in
                                  ["unit": "context\(i)", "source": p.source]
                              }, "glossary": req.glossary])
                } catch { state.value.withLock { $0.requests.removeValue(forKey: id) }?.resume(throwing: error) }
                // Cancellation may race registration after the initial check.
                if Task.isCancelled {
                    state.value.withLock { $0.requests.removeValue(forKey: id) }?.resume(throwing: CancellationError())
                }
            }
        } onCancel: {
            self.state.value.withLock { $0.requests.removeValue(forKey: id) }?.resume(throwing: CancellationError())
        }
        try Task.checkCancellation()
        return TranslationCandidate(unitID: req.unitID, text: text)
    }

    public func shutdown() {
        Self.failAll(state)
        try? input.fileHandleForWriting.close()
        if process.isRunning { process.terminate() }
    }

    deinit { shutdown() }
}
