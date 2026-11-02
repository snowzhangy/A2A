// Resolve a source application to the audio process objects that belong to it (A1).
// Helpers need not share the main bundle ID; membership is audited through the app
// bundle that contains each executable. Our own processes are always excluded and
// ambiguous selections are rejected rather than guessed.

import CoreAudio
import Foundation

public struct ResolvedSource: Sendable {
    public struct Member: Sendable, Hashable {
        public let process: AudioProcessInfo
        public let reason: String
    }

    public let bundleID: String
    public let appRoot: String?
    public let members: [Member]
    public let excluded: [Member]
    /// A global tap: the machine's whole output mix, so there is no app to choose. `members` then names
    /// nothing and `excluded` holds the processes the tap leaves out — ours, so the mixer never hears its
    /// own English coming back.
    public var global = false

    public var objectIDs: [AudioObjectID] { members.map(\.process.objectID) }

    /// True when every process this source names has exited. A global source names none — whoever is
    /// playing may come and go — and an empty list would make `allSatisfy` vacuously true, so both answer
    /// "not gone"; for the whole mix only a capture stall says anything.
    public func processesAllExited(liveness: (pid_t) -> Bool = { kill($0, 0) != 0 && errno == ESRCH }) -> Bool {
        guard !global, !members.isEmpty else { return false }
        return members.allSatisfy { liveness($0.process.pid) }
    }

    /// True when a capture with no frames is only a quiet machine. A global tap gets no callbacks at all
    /// until something plays, so frames are missing for a reason only while another process has a running
    /// output. A process tap is never idle in this sense: missing frames there are a stall, as before.
    public func mixIsIdle(processes: [AudioProcessInfo], selfPID: pid_t = getpid(),
                          selfBundleRoot: String? = SourceResolver.ownAppRoot()) -> Bool {
        guard global else { return false }
        return !processes.contains {
            $0.isRunningOutput && !SourceResolver.isSelf($0, selfPID: selfPID, selfBundleRoot: selfBundleRoot)
        }
    }
}

public enum SourceResolutionError: Error, CustomStringConvertible {
    case notFound(String)
    case ambiguous(String, [String])
    case selfTarget

    public var description: String {
        switch self {
        case .notFound(let s): "no audio process for \(s); is it running and has it opened audio output?"
        case .ambiguous(let s, let roots): "\(s) resolves to several app bundles: \(roots.joined(separator: ", ")); pass --pid"
        case .selfTarget: "refusing to tap our own process"
        }
    }
}

public enum SourceResolver {
    /// Resolve by bundle identifier against a process snapshot.
    public static func resolve(bundleID: String, processes: [AudioProcessInfo],
                               selfPID: pid_t = getpid(), selfBundleRoot: String? = Bundle.main.bundlePath,
                               bundleIDAtRoot: (String) -> String? = { Bundle(path: $0)?.bundleIdentifier }) throws -> ResolvedSource {
        // Some apps open audio only in a helper. Resolve its outer app's actual bundle
        // metadata instead of requiring the UI process to appear in the audio snapshot.
        let mains = processes.filter {
            $0.bundleID == bundleID || $0.appBundleRoot.flatMap(bundleIDAtRoot) == bundleID
        }
        guard !mains.isEmpty else { throw SourceResolutionError.notFound(bundleID) }
        let roots = Set(mains.compactMap(\.appBundleRoot))
        if roots.count > 1 { throw SourceResolutionError.ambiguous(bundleID, roots.sorted()) }
        let root = roots.first
        let mainPIDs = Set(mains.map(\.pid))

        var members: [ResolvedSource.Member] = []
        var excluded: [ResolvedSource.Member] = []
        for p in processes {
            let reason: String?
            if p.bundleID == bundleID {
                reason = "bundle id"
            } else if let root, p.executablePath.hasPrefix(root + "/") {
                reason = "inside \(root)"
            } else if mainPIDs.contains(p.parentPID), p.appBundleRoot == nil {
                reason = "child of \(p.parentPID) without its own app bundle"
            } else {
                reason = nil
            }
            guard let reason else { continue }
            if isSelf(p, selfPID: selfPID, selfBundleRoot: selfBundleRoot) {
                excluded.append(.init(process: p, reason: "own process"))
            } else {
                members.append(.init(process: p, reason: reason))
            }
        }
        if members.isEmpty { throw excluded.isEmpty ? SourceResolutionError.notFound(bundleID) : SourceResolutionError.selfTarget }
        return ResolvedSource(bundleID: bundleID, appRoot: root, members: members, excluded: excluded)
    }

    /// Resolve a single explicit PID.
    public static func resolve(pid: pid_t, processes: [AudioProcessInfo], selfPID: pid_t = getpid()) throws -> ResolvedSource {
        guard pid != selfPID else { throw SourceResolutionError.selfTarget }
        guard let p = processes.first(where: { $0.pid == pid }) else { throw SourceResolutionError.notFound("pid \(pid)") }
        return ResolvedSource(bundleID: p.bundleID, appRoot: p.appBundleRoot,
                              members: [.init(process: p, reason: "explicit pid")], excluded: [])
    }

    /// The machine's output as the source. Every app is heard, including ones started after the session,
    /// so nothing has to be picked; our own processes are the tap's exclusion list.
    public static func systemAudio(processes: [AudioProcessInfo], selfPID: pid_t = getpid(),
                                   selfBundleRoot: String? = ownAppRoot()) -> ResolvedSource {
        let own = processes.filter { isSelf($0, selfPID: selfPID, selfBundleRoot: selfBundleRoot) }
        return ResolvedSource(bundleID: "system:audio", appRoot: nil, members: [],
                              excluded: own.map { ResolvedSource.Member(process: $0, reason: "our own output, kept out of the mix") },
                              global: true)
    }

    /// The `.app` holding this executable, if it lives in one. The app's helper runs from
    /// `A2A.app/Contents/Helpers`, where `Bundle.main` is not the app bundle itself.
    public static func ownAppRoot(_ executable: String? = nil) -> String? {
        if let executable { return appRoot(in: executable) }
        return appRoot(in: Bundle.main.bundlePath) ?? appRoot(in: Bundle.main.executablePath ?? "")
    }

    private static func appRoot(in path: String) -> String? {
        var prefix = ""
        for component in path.split(separator: "/") {
            prefix += "/" + component
            if component.hasSuffix(".app") { return prefix }
        }
        return nil
    }

    static func isSelf(_ p: AudioProcessInfo, selfPID: pid_t, selfBundleRoot: String?) -> Bool {
        if p.pid == selfPID { return true }
        if let selfBundleRoot, selfBundleRoot.hasSuffix(".app"), p.executablePath.hasPrefix(selfBundleRoot + "/") {
            return true
        }
        return false
    }
}
