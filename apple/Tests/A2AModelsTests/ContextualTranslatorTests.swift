import A2ACore
import Foundation
import Testing
@testable import A2AModels

@Suite struct ContextualTranslatorTests {
    private func worker(arguments: [String] = []) throws -> ContextualTranslator {
        let root = URL(fileURLWithPath: #filePath).deletingLastPathComponent()
            .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
        return try ContextualTranslator(python: URL(fileURLWithPath: "/usr/bin/python3"),
            worker: root.appendingPathComponent("research/live_dub_lab/context_worker.py"), arguments: ["--fake"] + arguments)
    }

    @Test func residentWorkerHandlesSeveralRequestsAndStopsCleanly() async throws {
        let translator = try worker()
        defer { translator.shutdown() }
        try await translator.waitUntilReady()
        for id in 1...2 {
            let result = try await translator.translate(TranslationRequest(unitID: UnitID(id), context: [],
                glossary: [:], uncommittedSource: "你好"))
            #expect(result.unitID == UnitID(id))
            #expect(result.text == "Hello.")
        }
        translator.shutdown()
        await #expect(throws: (any Error).self) {
            try await translator.translate(TranslationRequest(unitID: UnitID(3), context: [],
                glossary: [:], uncommittedSource: "你好"))
        }
    }

    @Test func cancellationDiscardsLateResponseWithoutBreakingNextRequest() async throws {
        let gate = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: gate) }
        let translator = try worker(arguments: ["--fake-gate", gate.path])
        defer { translator.shutdown() }
        try await translator.waitUntilReady()
        let task = Task {
            try await translator.translate(TranslationRequest(unitID: UnitID(1), context: [],
                glossary: [:], uncommittedSource: "你好"))
        }
        let deadline = ContinuousClock.now.advanced(by: .seconds(5))
        while translator.pendingRequestCount == 0, ContinuousClock.now < deadline {
            try await Task.sleep(for: .milliseconds(1))
        }
        #expect(translator.pendingRequestCount == 1)
        task.cancel()
        await #expect(throws: CancellationError.self) { try await task.value }
        try Data().write(to: gate)
        let next = try await translator.translate(TranslationRequest(unitID: UnitID(2), context: [],
            glossary: [:], uncommittedSource: "再见"))
        #expect(next.text == "Hello.")
    }
}
