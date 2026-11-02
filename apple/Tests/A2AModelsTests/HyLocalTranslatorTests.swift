// Hy worker protocol contract, against research/hy_worker/fake_worker.py (standard library; no model).
// The real worker runs only on request: hy-worker --model GGUF --test WAV (scripts/build_hy_local.sh).

import A2ACore
import A2AMedia
import A2AModels
import Foundation
import Testing

private let repository = URL(fileURLWithPath: #filePath)
    .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
private let python = "/usr/bin/python3"
private let worker = repository.appendingPathComponent("research/hy_worker/fake_worker.py").path

private func fakeWorker(_ extra: [String] = [], direction: String = "zh-en") async throws -> HyLocalTranslator {
    try await HyLocalTranslator(executable: URL(fileURLWithPath: python), arguments: [worker] + extra,
                                direction: direction, readyTimeout: .seconds(30))
}

private func tone(seconds: Double) -> [Float] {
    (0..<Int(seconds * 48_000)).map { Float(0.1 * sin(Double($0) / 9)) }
}

@Suite(.enabled(if: FileManager.default.isExecutableFile(atPath: python)), .serialized)
struct HyLocalTranslatorTests {
    @Test func spansAnswerInOrderWithTheirOwnText() async throws {
        let hy = try await fakeWorker()
        #expect(hy.info?.model == "fake-hy")
        let first = try await hy.translate(unit: UnitID(1), samples: tone(seconds: 3))
        let second = try await hy.translate(unit: UnitID(2), samples: tone(seconds: 1.5))
        #expect(first.unitID == UnitID(1) && first.text == "3.0 s zh-en")
        #expect(second.unitID == UnitID(2) && second.text == "1.5 s zh-en")
        #expect(first.complete)
    }

    @Test func aStreamedSpanIsAnsweredWholeAndInOrder() async throws {
        let hy = try await fakeWorker()
        let audio = tone(seconds: 2.5)
        // The span arrives while it is spoken, then closes with its rest.
        hy.stream(Array(audio[0..<48_000]))
        hy.stream(Array(audio[48_000..<96_000]))
        let first = hy.close(remaining: Array(audio[96_000...]), spanSamples: audio.count)
        // A span that was dropped before it closed is not part of the next answer.
        hy.stream(tone(seconds: 0.2))
        hy.discard()
        hy.stream(tone(seconds: 1))
        let second = hy.close(spanSamples: 48_000)
        // Answers that arrive before they are asked for wait for their request.
        try await Task.sleep(for: .milliseconds(300))
        let b = try await hy.answer(second, unit: UnitID(2))
        let a = try await hy.answer(first, unit: UnitID(1))
        #expect(a.unitID == UnitID(1) && a.text == "2.5 s zh-en")
        #expect(b.unitID == UnitID(2) && b.text == "1.0 s zh-en")
        #expect(a.workerStats == TranslationWorkerStats(afterCloseMs: 0, turns: 1, sentenceFallback: false))
        #expect(hy.info?.streamsSpans == true)
        let older = try await fakeWorker(["--protocol-1"])
        #expect(older.info?.streamsSpans == false)
    }

    @Test func silenceIsAnEmptyCandidateNotAnError() async throws {
        let hy = try await fakeWorker(direction: "en-zh")
        let result = try await hy.translate(unit: UnitID(1), samples: [Float](repeating: 0, count: 48_000))
        #expect(result.text.isEmpty)
    }

    @Test func aDeadWorkerFailsTheSpanAndEveryLaterOne() async throws {
        let hy = try await fakeWorker(["--die-on-flush"])
        await #expect(throws: (any Error).self) { try await hy.translate(unit: UnitID(1), samples: tone(seconds: 1)) }
        #expect(hy.failure != nil)
        await #expect(throws: (any Error).self) { try await hy.translate(unit: UnitID(2), samples: tone(seconds: 1)) }
    }

    @Test func aHungWorkerTimesOutInsteadOfStallingTheQueue() async throws {
        // A 1 s span may take max(4 s, 2 × span) before it fails, as for the LAN server.
        let hy = try await fakeWorker(["--slow", "10"])
        let start = ContinuousClock.now
        await #expect(throws: (any Error).self) { try await hy.translate(unit: UnitID(1), samples: tone(seconds: 1)) }
        #expect(ContinuousClock.now - start < .seconds(6))
    }

    @Test func invalidDirectionOrEmptySpanIsRejected() async throws {
        await #expect(throws: (any Error).self) { _ = try await fakeWorker(direction: "fr-en") }
        let hy = try await fakeWorker()
        await #expect(throws: (any Error).self) { try await hy.translate(unit: UnitID(1), samples: []) }
    }

    @Test func aWorkerThatNeverBecomesReadyFailsTheStart() async throws {
        await #expect(throws: (any Error).self) {
            _ = try await HyLocalTranslator(executable: URL(fileURLWithPath: "/bin/sleep"), arguments: ["30"],
                                            direction: "zh-en", readyTimeout: .seconds(1))
        }
    }
}

/// The real model, only on request: A2A_HY_WORKER=.local/hy-simulmt/bin/hy-worker A2A_HY_MODEL=<gguf>
/// A2A_HY_WAV=<48 kHz mono float WAV of speech> swift test --filter HyLocalTranslatorRealModel
@Suite(.enabled(if: ["A2A_HY_WORKER", "A2A_HY_MODEL", "A2A_HY_WAV"].allSatisfy { ProcessInfo.processInfo.environment[$0] != nil }))
struct HyLocalTranslatorRealModel {
    @Test func translatesSpeechAndKeepsContextAcrossSpans() async throws {
        let env = ProcessInfo.processInfo.environment
        let hy = try await HyLocalTranslator(worker: URL(fileURLWithPath: env["A2A_HY_WORKER"]!),
                                             model: URL(fileURLWithPath: env["A2A_HY_MODEL"]!), direction: "zh-en")
        let wav = try WAV.decode(Data(contentsOf: URL(fileURLWithPath: env["A2A_HY_WAV"]!)))
        let samples = wav.mono()
        var texts: [String] = []
        var offset = 0
        while offset + 48_000 <= samples.count, texts.count < 6 {
            let span = Array(samples[offset..<min(offset + 3 * 48_000, samples.count)])
            let start = ContinuousClock.now
            let result = try await hy.translate(unit: UnitID(texts.count + 1), samples: span)
            print("span \(texts.count + 1): \(ContinuousClock.now - start), \(result.text.count) characters")
            texts.append(result.text)
            offset += span.count
        }
        #expect(texts.contains { !$0.isEmpty })
    }
}
