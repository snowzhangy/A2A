import Foundation

/// Where Hy-SimulMT on this Mac lives in a repository: the native worker that
/// scripts/build_hy_local.sh builds and the GGUF that scripts/fetch_models.py promotes into a
/// revision directory. The app checks these before a session; a2a-pipeline defaults to them.
public enum HyLocalSetup {
    public static let workerPath = ".local/hy-simulmt/bin/hy-worker"
    static let modelDirectory = ".local/models/hy-simulmt-30b-a3b-q4km-gguf"
    static let modelFile = "Hy-SimulMT-30B-A3B-Q4_K_M.gguf"

    public static func worker(root: URL) -> URL { root.appending(path: workerPath) }

    /// The GGUF in the fetched revision directory; with several, the last in name order.
    public static func model(root: URL) -> URL? {
        let base = root.appending(path: modelDirectory)
        let revisions = (try? FileManager.default.contentsOfDirectory(atPath: base.path)) ?? []
        return revisions.sorted().reversed().lazy.map { base.appending(path: "\($0)/\(modelFile)") }
            .first { FileManager.default.fileExists(atPath: $0.path) }
    }

    /// What the repository at `root` still needs, as an instruction for the user; nil when ready.
    public static func missing(root: URL) -> String? {
        guard FileManager.default.isExecutableFile(atPath: worker(root: root).path) else {
            return "Build the Hy-SimulMT worker with scripts/build_hy_local.sh."
        }
        guard model(root: root) != nil else {
            return "Fetch the model with: uv run --project harness python scripts/fetch_models.py --models hy-simulmt-30b-a3b-q4km-gguf"
        }
        return nil
    }
}
