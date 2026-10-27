// Opt-in research voice references. Kept in a private local directory, outside diagnostics.
import A2ACore
import A2AMedia
import CryptoKit
import Foundation
import Synchronization

public struct RememberedVoice: Codable, Sendable {
    public let id: UUID
    private let personID: UUID?
    fileprivate let referenceVersion: Int?
    public var identityID: UUID { personID ?? id }
    public let voiceprint: Voiceprint
    private let sampleRate: Int
    private let pcm: Data
    private let text: String

    fileprivate init(id: UUID, profile: VoiceProfile, voiceprint: Voiceprint) {
        self.id = profile.version == 1 ? id : UUID()
        personID = id
        referenceVersion = profile.version
        self.voiceprint = voiceprint
        sampleRate = profile.sampleRate
        text = profile.referenceText
        pcm = profile.referencePCM.withUnsafeBytes { Data($0) }
    }

    fileprivate var valid: Bool {
        voiceprint.isValid && (16_000...96_000).contains(sampleRate)
            && pcm.count % 4 == 0 && pcm.count >= sampleRate * 4 && pcm.count <= sampleRate * 4 * 15
            && !text.isEmpty && text.utf8.count <= 32_768
    }

    public func profile(for speaker: SpeakerID) -> VoiceProfile? {
        guard valid else { return nil }
        let samples: [Float] = pcm.withUnsafeBytes { raw in
            stride(from: 0, to: raw.count, by: 4).map { Float(bitPattern: raw.loadUnaligned(fromByteOffset: $0, as: UInt32.self)) }
        }
        guard samples.allSatisfy(\.isFinite) else { return nil }
        return VoiceProfile(speakerID: speaker, version: 1, referencePCM: samples, referenceText: text,
                            sampleRate: sampleRate, channelCount: 1,
                            quality: ReferenceQuality(durationSeconds: Double(samples.count) / Double(sampleRate),
                                                      snrEstimateDB: .nan, clippedFraction: 0, overlapSuspected: false))
    }
}

public final class VoiceMemoryStore: @unchecked Sendable {
    public static let maximumVoices = 16
    public let directory: URL
    private let voices: Mutex<[RememberedVoice]>
    private let writes = DispatchQueue(label: "a2a.voice-memory", qos: .utility)
    private let writeFailure = Mutex(false)

    public static func directory(root: URL, source: String) -> URL {
        let key = SHA256.hash(data: Data(source.utf8)).map { String(format: "%02x", $0) }.joined()
        return root.appendingPathComponent(key, isDirectory: true)
    }

    public init(directory: URL) throws {
        self.directory = directory
        let fm = FileManager.default
        try fm.createDirectory(at: directory, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
        guard try directory.resourceValues(forKeys: [.isSymbolicLinkKey]).isSymbolicLink != true else {
            throw AdapterError.unsupported("voice memory directory must not be a symbolic link")
        }
        try fm.setAttributes([.posixPermissions: 0o700], ofItemAtPath: directory.path)
        var loaded: [RememberedVoice] = []
        for url in try fm.contentsOfDirectory(at: directory, includingPropertiesForKeys: [.fileSizeKey, .isSymbolicLinkKey])
            .filter({ $0.pathExtension == "voice" }).sorted(by: { $0.lastPathComponent < $1.lastPathComponent }).prefix(Self.maximumVoices * 2) {
            guard let values = try? url.resourceValues(forKeys: [.fileSizeKey, .isSymbolicLinkKey]),
                  values.isSymbolicLink != true, (values.fileSize ?? Int.max) <= 6_000_000,
                  let data = try? Data(contentsOf: url),
                  let item = try? PropertyListDecoder().decode(RememberedVoice.self, from: data), item.valid,
                  item.id.uuidString == url.deletingPathExtension().lastPathComponent else { continue }
            loaded.append(item)
        }
        let identities = Array(Set(loaded.map(\.identityID))).sorted { $0.uuidString < $1.uuidString }.prefix(Self.maximumVoices)
        voices = Mutex(loaded.filter { identities.contains($0.identityID) })
    }

    public var count: Int { voices.withLock { Set($0.map(\.identityID)).count } }
    public var saveFailed: Bool { writeFailure.withLock { $0 } }

    /// Reject weak and ambiguous matches; session-local speaker numbers never select a voice.
    public func match(_ print: Voiceprint) -> RememberedVoice? {
        guard print.isValid, print.voicedFrames >= 40 else { return nil }
        return voices.withLock { list in
            let candidates = list.compactMap { item -> (RememberedVoice, Double)? in
                if let a = print.medianPitchHz, let b = item.voiceprint.medianPitchHz,
                   abs(log2(a / b)) > 0.4 { return nil }
                return (item, item.voiceprint.distance(to: print))
            }
            // Two references of one person are evidence, not competing identities.
            let ranked = Dictionary(grouping: candidates, by: { $0.0.identityID }).values
                .compactMap { $0.min(by: { $0.1 < $1.1 }) }.sorted { $0.1 < $1.1 }
            guard let best = ranked.first, best.1 <= 0.25,
                  ranked.count == 1 || ranked[1].1 - best.1 >= 0.08 else { return nil }
            return best.0
        }
    }

    public func remember(_ profile: VoiceProfile, id: UUID) {
        guard profile.channelCount == 1, (16_000...96_000).contains(profile.sampleRate),
              (1...2).contains(profile.version),
              profile.referencePCM.count <= profile.sampleRate * 15,
              profile.referencePCM.allSatisfy(\.isFinite), profile.referenceText.utf8.count <= 32_768 else { return }
        // The reference itself defines the saved print, rather than a possibly mixed cluster.
        let samples = StreamingResampler.resample(profile.referencePCM, from: profile.sampleRate, to: 16_000)
        var analyzer = VoiceprintAnalyzer()
        var print: Voiceprint? = Voiceprint()
        analyzer.process(samples, into: &print)
        guard let print, print.isValid, print.voicedFrames >= 40 else { return }
        let item = RememberedVoice(id: id, profile: profile, voiceprint: print)
        guard item.valid else { return }
        writes.async { [self] in
            do {
                let encoder = PropertyListEncoder()
                encoder.outputFormat = .binary
                let url = directory.appendingPathComponent(item.id.uuidString + ".voice")
                try encoder.encode(item).write(to: url, options: .atomic)
                try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: url.path)
                let evicted: [RememberedVoice] = voices.withLock { list in
                    var old = list.filter { $0.identityID == item.identityID && $0.referenceVersion == item.referenceVersion && $0.id != item.id }
                    list.removeAll { $0.identityID == item.identityID && $0.referenceVersion == item.referenceVersion }
                    list.append(item)
                    while Set(list.map(\.identityID)).count > Self.maximumVoices, let first = list.first {
                        old += list.filter { $0.identityID == first.identityID }
                        list.removeAll { $0.identityID == first.identityID }
                    }
                    return old
                }
                for old in evicted { try? FileManager.default.removeItem(at: directory.appendingPathComponent(old.id.uuidString + ".voice")) }
            } catch { writeFailure.withLock { $0 = true } }
        }
    }

    /// Called during ordinary-thread session teardown, after reference tasks have ended.
    public func flush() { writes.sync {} }
}
