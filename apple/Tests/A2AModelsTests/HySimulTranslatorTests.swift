import A2ACore
@testable import A2AModels
import Foundation
import Testing

@Test func hyEventsDiscardDiagnostics() throws {
    let stream = """
    data: {"delta":"Hello everyone,"}

    data: {"delta":"today we meet."}

    data: {"delta":"run_turn_stream: 22 tokens, 5.00 s audio"}

    data: {"done":true,"code":0,"text":"Hello everyone, today we meet. run_turn_stream: 22 tokens"}

    """
    let result = try HySimulTranslator.parseEvents(stream, unit: UnitID(1))
    #expect(result.text == "Hello everyone, today we meet.")
}

@Test func hyEventsRejectFailureAndIncompleteResponse() {
    #expect(throws: (any Error).self) {
        try HySimulTranslator.parseEvents("data: {\"done\":true,\"code\":1,\"text\":\"\"}", unit: UnitID(1))
    }
    #expect(throws: (any Error).self) {
        try HySimulTranslator.parseEvents("data: {\"delta\":\"incomplete\"}", unit: UnitID(1))
    }
}

@Test func hyCompletedEmptyAnswerIsAnEmptyCandidateRatherThanAnError() throws {
    // Music, laughter, or a pause: the server finishes with nothing to say. The reducer turns
    // an empty candidate into silence, which is not a failed translation.
    let stream = """
    data: {"done":true,"code":0,"text":""}

    """
    let result = try HySimulTranslator.parseEvents(stream, unit: UnitID(1))
    #expect(result.text.isEmpty)
    #expect(result.complete)
}

@Test func hyFinishedTextIsUsedWhenNoDeltasArrive() throws {
    let stream = """
    data: {"done":true,"code":0,"text":"Hello there. run_turn_stream: 4 tokens"}

    """
    #expect(try HySimulTranslator.parseEvents(stream, unit: UnitID(1)).text == "Hello there.")
}

@Test func hyHealthGatesReadinessOnlyWhenTheServerReportsResidency() {
    func health(_ json: String) -> Data { Data(json.utf8) }
    // The 1 November warm-up: the root answered while the model was still loading.
    #expect(HySimulTranslator.residency(health: health(#"{"status":"starting","resident":"starting"}"#)) == .loading)
    #expect(HySimulTranslator.residency(health: health(#"{"status":"ok","resident":"ready","busy":true}"#)) == .ready)
    #expect(HySimulTranslator.residency(health: health(#"{"status":"failed","resident":"failed"}"#)) == .failed)
    #expect(HySimulTranslator.residency(health: health(#"{"resident":true}"#)) == .loading)
    // Servers without the field, without JSON, or without /health pass as before.
    #expect(HySimulTranslator.residency(health: health(#"{"status":"ok"}"#)) == .ready)
    #expect(HySimulTranslator.residency(health: health("<html>404</html>")) == .ready)
    #expect(HySimulTranslator.residency(health: nil) == .ready)
}

@Test func hyRequestTimeoutIsTwiceTheSpanWithAFloor() {
    #expect(HySimulTranslator.requestTimeout(samples: 48_000 * 3) == 6)
    #expect(HySimulTranslator.requestTimeout(samples: 12_000) == 4)
    #expect(HySimulTranslator.requestTimeout(samples: 48_000 * 30) == 60)
}

@Test func hyLocalSetupNamesTheFirstMissingPieceAndFindsTheFetchedModel() throws {
    let files = FileManager.default
    let root = files.temporaryDirectory.appending(path: "a2a-hy-\(UUID().uuidString)")
    defer { try? files.removeItem(at: root) }
    func place(_ path: String, executable: Bool = false) throws {
        let url = root.appending(path: path)
        try files.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
        try Data().write(to: url)
        if executable { try files.setAttributes([.posixPermissions: 0o755], ofItemAtPath: url.path) }
    }
    #expect(HyLocalSetup.missing(root: root)?.contains("scripts/build_hy_local.sh") == true)
    try place(".local/hy-simulmt/bin/hy-worker", executable: true)
    #expect(HyLocalSetup.missing(root: root)?.contains("--models hy-simulmt-30b-a3b-q4km-gguf") == true)
    // A revision directory without the GGUF (an interrupted fetch never promotes one) is skipped.
    try files.createDirectory(at: root.appending(path: ".local/models/hy-simulmt-30b-a3b-q4km-gguf/ffff"), withIntermediateDirectories: true)
    try place(".local/models/hy-simulmt-30b-a3b-q4km-gguf/8d4e938/Hy-SimulMT-30B-A3B-Q4_K_M.gguf")
    #expect(HyLocalSetup.missing(root: root) == nil)
    #expect(HyLocalSetup.model(root: root)?.path.hasSuffix("8d4e938/Hy-SimulMT-30B-A3B-Q4_K_M.gguf") == true)
}
