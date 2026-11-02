import Foundation
import XCTest
@testable import A2AModels

final class QwenCloudProtocolTests: XCTestCase {
    func testVoiceCloningAndLanguageSessionConfiguration() throws {
        let event = QwenCloudProtocol.sessionUpdate(target: "en")
        XCTAssertEqual(event["type"] as? String, "session.update")
        let session = try XCTUnwrap(event["session"] as? [String: Any])
        XCTAssertEqual(session["output_modalities"] as? [String], ["text", "audio"])
        XCTAssertEqual((session["translation"] as? [String: String])?["language"], "en")
        XCTAssertEqual(session["enable_voice_clone"] as? Bool, true)
        XCTAssertEqual((session["voice_clone_options"] as? [String: String])?["frequency"], "always")
        XCTAssertEqual(session["voice"] as? String, "default")
    }

    func testPCMIsLittleEndianClippedAndSafeForNonfiniteInput() throws {
        let encoded = QwenCloudProtocol.encodePCM16([-1, 0, 1, 2, .nan])
        XCTAssertEqual(Array(encoded), [1, 128, 0, 0, 255, 127, 255, 127, 0, 0])
        let decoded = try XCTUnwrap(QwenCloudProtocol.decodePCM16(encoded))
        XCTAssertEqual(decoded[0], -32767.0 / 32768.0, accuracy: 0.00001)
        XCTAssertEqual(decoded[2], 32767.0 / 32768.0, accuracy: 0.00001)
        XCTAssertNil(QwenCloudProtocol.decodePCM16(Data([1])))
        XCTAssertNil(QwenCloudProtocol.decodePCM16(Data(repeating: 0, count: 1_048_578)))
    }
}
