import XCTest
@testable import A2ACore

final class ProviderStreamingTests: XCTestCase {
    func testProviderPlaybackPolicyGatesAndFlushesPCM() {
        var config = PipelineConfig(session: "synthetic")
        config.providerStreaming = true
        var pipeline = SessionPipeline(config: config)
        XCTAssertTrue(pipeline.handle(.providerPCM, at: 1).isEmpty)
        XCTAssertEqual(pipeline.handle(.arm, at: 2), [.rampGains(.translated, durationMs: 0)])
        XCTAssertEqual(pipeline.handle(.providerPCM, at: 3), [.enqueueProviderPCM])
        XCTAssertEqual(pipeline.handle(.playbackMode(.original), at: 4),
                       [.flushTranslatedBus, .rampGains(.original, durationMs: config.health.rampMs)])
        XCTAssertTrue(pipeline.handle(.providerPCM, at: 5).isEmpty)
        XCTAssertEqual(pipeline.handle(.playbackMode(.translated), at: 6),
                       [.flushTranslatedBus, .rampGains(.translated, durationMs: config.health.rampMs)])
        XCTAssertEqual(pipeline.handle(.providerPCM, at: 7), [.enqueueProviderPCM])
        XCTAssertEqual(pipeline.handle(.stop, at: 8), [.flushTranslatedBus, .teardown])
        XCTAssertTrue(pipeline.handle(.providerPCM, at: 9).isEmpty)
    }
}
