import A2AModels
import Foundation
import Testing

@Suite(.enabled(if: FileManager.default.isExecutableFile(atPath: "/usr/bin/python3")))
struct ExperimentalVoiceSetupTests {
    private func run(_ source: String) async throws -> [ExperimentalVoiceInstallation] {
        let folder = FileManager.default.temporaryDirectory.appendingPathComponent("a2a-model-setup-\(UUID())")
        try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: folder) }
        let script = folder.appendingPathComponent("setup.py")
        try source.write(to: script, atomically: true, encoding: .utf8)
        return try await ExperimentalVoiceSetup.run(python: "/usr/bin/python3", script: script.path)
    }

    @Test func setupProgressCannotBlockCatalogDelivery() async throws {
        let rows = try await run("""
            import json, sys
            sys.stderr.write('progress' * 32768)
            print(json.dumps([{'key':'qwen','installed':True,'bytes':3207975456}]))
            """)
        #expect(rows.count == 1 && rows[0].installed)
        #expect(rows[0].bytes == 3_207_975_456)
    }

    @Test func failedSetupDoesNotAcceptItsOutput() async {
        await #expect(throws: (any Error).self) {
            try await run("import sys; print('[]'); sys.exit(1)")
        }
    }

    @Test func oversizedCatalogIsStoppedBeforeDecoding() async {
        await #expect(throws: (any Error).self) {
            try await run("print(' ' * 100000)")
        }
    }
}
