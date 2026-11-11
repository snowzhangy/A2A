import A2AAudio
import A2ACore
import A2AMedia
import A2AModels
import A2AVideo
import AppKit
import Darwin
import Observation
import Synchronization

struct AudioSource: Identifiable {
    let id: String
    let name: String
    let arguments: [String]

    /// There is no app to choose: the tap hears the machine's whole output mix, so whatever is playing is
    /// translated, including an app started after us. The name keys the saved-voice store, whose identities
    /// are separated by voice rather than by which app played.
    static let systemAudio = AudioSource(id: "system", name: "System audio", arguments: ["--tap-global"])
}

struct CaptionLine: Identifiable {
    let id: String
    var text: String
}

private struct VoiceLearningProgress {
    let state: String
    let seconds: Double
    let reused: Bool
}

@MainActor @Observable final class SessionModel {
    var captions: [CaptionLine] = []
    var status = "Ready"
    var error: String?
    var running = false
    var stopping = false
    var checking = false
    var languagesReady = false
    var languageDetail = "Check the Hy-SimulMT server."
    var hyServer = UserDefaults.standard.string(forKey: "hyServer") ?? "http://127.0.0.1:8082" {
        didSet {
            UserDefaults.standard.set(hyServer, forKey: "hyServer")
            if hyServer != oldValue, !hyOnThisMac { languagesReady = false; languageDetail = "Check the Hy-SimulMT server."; languageCheck = UUID() }
        }
    }
    /// Hy-SimulMT runs on this Mac (each session starts the native worker) unless a server is chosen.
    var hyOnThisMac = UserDefaults.standard.object(forKey: "hyOnThisMac") as? Bool ?? true {
        didSet {
            guard hyOnThisMac != oldValue else { return }
            UserDefaults.standard.set(hyOnThisMac, forKey: "hyOnThisMac")
            languagesReady = false
            languageCheck = UUID()
            Task { await checkLanguages() }
        }
    }
    var direction: TranslationDirection = .chineseToEnglish {
        didSet {
            guard direction != oldValue else { return }
            languagesReady = false
            languageDetail = "Check the Hy-SimulMT server."
            captions.removeAll()
            error = nil
        }
    }
    var qualityFirst = false
    var keepBackground = true
    /// A single Qwen worker uses less memory but cannot synthesize two lines at once.
    var reduceVoiceMemory = UserDefaults.standard.object(forKey: "reduceVoiceMemory") as? Bool ?? false {
        didSet {
            if !CommandLine.arguments.contains("--smoke-output") { UserDefaults.standard.set(reduceVoiceMemory, forKey: "reduceVoiceMemory") }
        }
    }
    /// Use one pinned voice unless the listener explicitly selects dialogue tracking.
    var multipleSpeakers = UserDefaults.standard.object(forKey: "multipleSpeakers") as? Bool ?? false {
        didSet {
            if !CommandLine.arguments.contains("--smoke-output") { UserDefaults.standard.set(multipleSpeakers, forKey: "multipleSpeakers") }
            if multipleSpeakers { voiceReferenceURL = nil }
        }
    }
    /// Optional explicit reference for one speaker; it is not persisted or copied by the app.
    var voiceReferenceURL: URL?
    /// ADR-0012: show the chosen window's picture D seconds late and give every translated
    /// phrase, the original audio and the separated background the same delay, so speech lands
    /// on its picture instead of trailing it by a varying amount.
    var matchPicture = UserDefaults.standard.object(forKey: "matchPicture") as? Bool ?? true {
        didSet {
            if !CommandLine.arguments.contains("--smoke-output") { UserDefaults.standard.set(matchPicture, forKey: "matchPicture") }
        }
    }
    /// D. Measured live onset was 4.9 s median and 6.3 s at most with streaming speech
    /// (research/live_dub_lab/SOUNDTRACK-AND-DELAY.md), so 8 s leaves most phrases on time.
    var pictureDelaySeconds = UserDefaults.standard.object(forKey: "pictureDelaySeconds") as? Double ?? 8 {
        didSet { UserDefaults.standard.set(pictureDelaySeconds, forKey: "pictureDelaySeconds") }
    }
    /// Live fine-tune of the picture only (output-device and display latency differ per setup).
    var pictureOffsetMs = UserDefaults.standard.object(forKey: "pictureOffsetMs") as? Double ?? 0 {
        didSet {
            UserDefaults.standard.set(pictureOffsetMs, forKey: "pictureOffsetMs")
            mirror?.setOffset(seconds: pictureOffsetMs / 1_000)
        }
    }
    var choosingPicture = false
    private var mirror: DelayedMirror?
    private var pictureWindow: PictureWindowController?
    /// The delay the running session was started with; 0 without a delayed picture.
    var sessionPictureDelayMs = 0
    var rememberedVoiceCount = 0
    private var voiceLearning: [String: VoiceLearningProgress] = [:]
    var voiceLearningDetail: String {
        guard running, !voiceLearning.isEmpty else { return "" }
        return voiceLearning.keys.sorted().compactMap { speaker in
            guard let p = voiceLearning[speaker] else { return nil }
            switch p.state {
            case "ready": return "\(speaker): \(p.reused ? "saved voice ready" : "voice ready")"
            case "preparing": return "\(speaker): preparing \(p.reused ? "saved voice" : "voice")"
            default: return "\(speaker): collecting \(String(format: "%.1f", p.seconds))/3.0 s"
            }
        }.joined(separator: " · ")
    }
    var voiceInstallations: [ExperimentalVoiceInstallation] = []
    var checkingVoiceModels = false
    var settingUp = false
    var setupDetail = ""
    var voiceSetupDetail = "Checking voice model…"
    var selectedVoiceInstallation: ExperimentalVoiceInstallation? {
        voiceInstallations.first { $0.key == ExperimentalVoiceModel.qwen.rawValue }
    }
    var voiceReady: Bool { selectedVoiceInstallation?.installed == true && cloneWorker != nil }
    var languageReadyForSession: Bool { languagesReady }
    var playbackMode: PlaybackMode = .translated {
        didSet {
            guard playbackMode != oldValue else { return }
            sendPlaybackMode()
        }
    }
    let repository = SessionModel.findRepository()
    /// The model the last check found; sessions pass it so they need not look again.
    private var hyLocalModel: URL?
    var cloneWorker: (python: String, script: String)? { SessionModel.findCloneWorker() }
    private var process: Process?
    private var restartAfterSpeakerReset = false
    private var outputPipe: Pipe?
    private var errorPipe: Pipe?
    private var controlPipe: Pipe?
    private var setupProcess: Process?
    private var setupCancelled = false
    private var pending = Data()
    private var generation = UUID()
    private var traceURL: URL?
    private var languageCheck = UUID()

    private var voiceMemoryDirectory: URL? {
        guard let cloneWorker else { return nil }
        let root = URL(fileURLWithPath: cloneWorker.script).deletingLastPathComponent().deletingLastPathComponent()
            .appendingPathComponent(".local/voice-profiles", isDirectory: true)
        return VoiceMemoryStore.directory(root: root, source: AudioSource.systemAudio.name)
    }

    func refreshVoiceMemoryCount() {
        guard let directory = voiceMemoryDirectory else { rememberedVoiceCount = 0; return }
        Task { [weak self] in
            let count = await Task.detached {
                guard FileManager.default.fileExists(atPath: directory.path) else { return 0 }
                return (try? VoiceMemoryStore(directory: directory).count) ?? 0
            }.value
            guard let self, self.voiceMemoryDirectory == directory else { return }
            self.rememberedVoiceCount = count
        }
    }

    func forgetVoices() {
        guard !running, let directory = voiceMemoryDirectory else { return }
        do {
            if FileManager.default.fileExists(atPath: directory.path) { try FileManager.default.removeItem(at: directory) }
            refreshVoiceMemoryCount()
        } catch { self.error = String(describing: error) }
    }

    func refreshVoiceModels() async {
        guard let cloneWorker else {
            voiceSetupDetail = "Use Set up models to prepare the voice environment."
            return
        }
        checkingVoiceModels = true
        defer { checkingVoiceModels = false }
        do {
            let script = URL(fileURLWithPath: cloneWorker.script).deletingLastPathComponent()
                .appendingPathComponent("voice_models.py").path
            voiceInstallations = try await ExperimentalVoiceSetup.run(python: cloneWorker.python, script: script)
            voiceSetupDetail = ""
        } catch { voiceSetupDetail = String(describing: error) }
    }

    /// Explicit setup only: the session itself never downloads models. The script verifies and
    /// installs the selected local artifacts and keeps the large Hy model off server-only Macs.
    func setupModels(serverOnlyOverride: Bool? = nil) async {
        guard !running, !settingUp, let repository else { return }
        let serverOnly = serverOnlyOverride ?? !hyOnThisMac
        settingUp = true
        setupCancelled = false
        error = nil
        setupDetail = "Preparing models. This can take a while…"
        defer { settingUp = false; setupProcess = nil }
        let process = Process()
        // A separate process group lets Cancel stop both setup and its download/build children.
        process.executableURL = URL(fileURLWithPath: "/usr/bin/python3")
        process.arguments = ["-c", "import os,sys; os.setsid() if os.getpgrp() != os.getpid() else None; os.execv('/bin/bash', ['/bin/bash'] + sys.argv[1:])",
                             repository.appendingPathComponent("scripts/setup.sh").path] + (serverOnly ? ["--server"] : [])
        process.currentDirectoryURL = repository
        var environment = ProcessInfo.processInfo.environment
        environment["PATH"] = "/opt/homebrew/bin:/usr/local/bin:" + (environment["PATH"] ?? "/usr/bin:/bin:/usr/sbin:/sbin")
        process.environment = environment
        let output = Pipe()
        let bytes = Mutex(Data())
        output.fileHandleForReading.readabilityHandler = { handle in
            let data = handle.availableData
            if data.isEmpty { handle.readabilityHandler = nil; return }
            bytes.withLock {
                $0.append(data)
                if $0.count > 8_192 { $0.removeFirst($0.count - 8_192) }
            }
        }
        process.standardOutput = output
        process.standardError = output
        process.standardInput = FileHandle.nullDevice
        defer { output.fileHandleForReading.readabilityHandler = nil }
        do { try process.run() }
        catch { setupDetail = "Setup could not start."; self.error = String(describing: error); return }
        setupProcess = process
        let result = await Task.detached { () -> (Bool, String) in
            process.waitUntilExit()
            return (process.terminationStatus == 0, bytes.withLock { String(decoding: $0, as: UTF8.self) })
        }.value
        if setupCancelled {
            setupDetail = "Setup canceled. You can retry."
        } else if result.0 {
            setupDetail = "Setup complete."
            await checkLanguages()
            await refreshVoiceModels()
        } else {
            setupDetail = "Setup could not finish. You can retry."
            let detail = result.1.trimmingCharacters(in: .whitespacesAndNewlines)
            error = detail.isEmpty ? "Model setup failed." : String(detail.suffix(2_000))
        }
    }

    func cancelSetup() {
        guard settingUp, let process = setupProcess, process.isRunning else { return }
        setupCancelled = true
        setupDetail = "Stopping setup…"
        if kill(-process.processIdentifier, SIGTERM) != 0 { process.terminate() }
    }

    func checkLanguages() async {
        if hyOnThisMac {
            // Nothing to reach: the session starts the worker, so readiness is its files. Listing
            // a folder under ~/Documents can wait on a privacy prompt, so never on the main thread.
            let token = UUID()
            languageCheck = token
            let repository = repository
            let (missing, model) = await Task.detached { () -> (String?, URL?) in
                guard let repository else { return ("This copy of A2A is not inside its repository.", nil) }
                return (HyLocalSetup.missing(root: repository), HyLocalSetup.model(root: repository))
            }.value
            guard languageCheck == token, hyOnThisMac else { return }
            hyLocalModel = model
            languagesReady = missing == nil
            languageDetail = missing == nil ? "Hy-SimulMT is ready on this Mac." :
                (repository == nil ? "Keep A2A inside its source folder to set it up." : "Hy-SimulMT is not set up on this Mac.")
            return
        }
        let server = hyServer
        let token = UUID()
        languageCheck = token
        checking = true
        defer { if languageCheck == token { checking = false } }
        var rootStatus: Int?
        var residency = HySimulTranslator.Residency.ready
        if let url = URL(string: server), ["http", "https"].contains(url.scheme?.lowercased() ?? ""), url.host != nil {
            var request = URLRequest(url: url)
            request.timeoutInterval = 5
            rootStatus = (try? await URLSession.shared.data(for: request).1 as? HTTPURLResponse)?.statusCode
            if rootStatus != nil {
                // The root can answer while the model is still loading; spans sent then expire
                // (1 November live session: 33 s lost). /health says when it is resident.
                var health = URLRequest(url: url.appending(path: "health"))
                health.timeoutInterval = 5
                residency = HySimulTranslator.residency(health: try? await URLSession.shared.data(for: health).0)
            }
        }
        guard languageCheck == token, server == hyServer else { return }
        languagesReady = rootStatus == 200 && residency == .ready
        switch residency {
        case .loading: languageDetail = "Hy-SimulMT is still loading its model. Check the server again in a moment."
        case .failed: languageDetail = "Hy-SimulMT could not load its model. Check the server."
        case .ready: languageDetail = languagesReady ? "Hy-SimulMT is reachable. Audio is translated directly on your network."
                                                     : "Check that the Hy-SimulMT server is reachable."
        }
    }

    /// The development app lives in the repository's .local/apps.
    static func findRepository() -> URL? {
        var dir = Bundle.main.bundleURL.deletingLastPathComponent()
        while dir.path != "/" {
            if FileManager.default.fileExists(atPath: dir.appendingPathComponent("research/tts_worker.py").path) { return dir }
            dir = dir.deletingLastPathComponent()
        }
        return nil
    }

    /// The research voice worker and its environment, which sit above the app.
    static func findCloneWorker() -> (python: String, script: String)? {
        guard let root = findRepository() else { return nil }
        let script = root.appendingPathComponent("research/tts_worker.py").path
        let python = root.appendingPathComponent("research/.venv/bin/python").path
        return FileManager.default.isExecutableFile(atPath: python) ? (python, script) : nil
    }

    func start() {
        guard process == nil, !choosingPicture, languageReadyForSession, voiceReady, !settingUp else { return }
        guard matchPicture else {
            sessionPictureDelayMs = 0
            launchPipeline()
            return
        }
        // Refuse before the window picker, not after the person has chosen a window.
        if let failure = preflightFailure() {
            error = failure
            return
        }
        error = nil
        choosingPicture = true
        status = "Choose the window that shows the video…"
        Task {
            defer { choosingPicture = false }
            do {
                let filter = try await WindowPicker().pick()
                let mirror = DelayedMirror(delaySeconds: pictureDelaySeconds, offsetSeconds: pictureOffsetMs / 1_000)
                // The model lives as long as the app; stopPicture drops both references.
                mirror.onFailure = { message in Task { @MainActor in self.pictureFailed(message) } }
                try await mirror.start(filter: filter)
                self.mirror = mirror
                let window = PictureWindowController(mirror: mirror, sourceSize: filter.contentRect.size) {
                    self.pictureWindowClosed()
                }
                pictureWindow = window
                window.showWindow(nil)
                sessionPictureDelayMs = Int((pictureDelaySeconds * 1_000).rounded())
                launchPipeline()
                if process == nil { await stopPicture() }
            } catch WindowPickerError.canceled {
                status = "Ready"
                error = "Choose the window that shows the video, or turn off “Match picture to translated speech”."
                await stopPicture()
            } catch {
                status = "Ready"
                self.error = "Could not show the delayed picture: \(error)"
                await stopPicture()
            }
        }
    }

    private func pictureFailed(_ message: String) {
        guard mirror != nil else { return }
        error = message + (running ? String(format: " Translated speech stays %.0f s behind; restart the session to continue without the picture.",
                                            Double(sessionPictureDelayMs) / 1_000) : "")
        Task { await stopPicture() }
    }

    /// Closing the picture leaves the session (and its delayed audio) running. The controller
    /// is released after its close callback returns, not inside it.
    private func pictureWindowClosed() {
        Task { await stopPicture(closeWindow: false) }
    }

    private func stopPicture(closeWindow: Bool = true) async {
        let mirror = self.mirror
        self.mirror = nil
        let controller = pictureWindow
        pictureWindow = nil
        if closeWindow, let window = controller?.window, window.isVisible { window.close() }
        await mirror?.stop()
        WindowPicker.deactivate()
        _ = controller
    }

    private var pipelineExecutable: URL {
        let args = CommandLine.arguments
        if args.contains("--smoke-output"), let i = args.firstIndex(of: "--smoke-pipeline"), i + 1 < args.count {
            return URL(fileURLWithPath: args[i + 1])
        }
        return Bundle.main.bundleURL.appendingPathComponent("Contents/Helpers/a2a-pipeline")
    }

    /// Used by the synthetic lifecycle smoke test; never includes audio or text.
    var pipelineProcessID: Int32? { process?.isRunning == true ? process?.processIdentifier : nil }

    /// Why a session cannot start, checked before the helper takes over audio.
    func preflightFailure() -> String? {
        guard FileManager.default.isExecutableFile(atPath: pipelineExecutable.path) else {
            return "The translation helper is missing. Build the app with scripts/build_app.sh."
        }
        if let reference = voiceReferenceURL {
            do {
                let values = try reference.resourceValues(forKeys: [.fileSizeKey, .isRegularFileKey])
                guard values.isRegularFile == true, let size = values.fileSize, size > 0, size <= 32_000_000 else {
                    return "Choose a WAV reference smaller than 32 MB."
                }
                let audio = try WAV.read(reference)
                guard audio.sampleRate > 0, (1...15).contains(audio.durationSeconds) else {
                    return "Choose a WAV reference between 1 and 15 seconds long."
                }
            } catch { return "Could not read the voice reference. Choose a valid WAV file." }
        }
        guard cloneWorker != nil else { return "Set up the voice environment with: uv sync --project research --locked" }
        return nil
    }

    private func launchPipeline() {
        guard process == nil else { return }
        let source = AudioSource.systemAudio
        let executable = pipelineExecutable
        // Validate before starting the helper and muting the source (again: the window
        // picker may have been open for a while).
        if let failure = preflightFailure() {
            error = failure
            return
        }
        error = nil
        captions.removeAll()
        voiceLearning.removeAll()
        pending.removeAll()
        generation = UUID()
        let token = generation
        let child = Process()
        let stdout = Pipe(), stderr = Pipe(), control = Pipe()
        // A helper exiting during a click must return EPIPE rather than terminate the app.
        guard fcntl(control.fileHandleForWriting.fileDescriptor, F_SETNOSIGPIPE, 1) == 0 else {
            error = "Could not prepare the playback switch. Please restart the app."
            return
        }
        // Diagnostics have no captions or audio and are removed when the session ends.
        let trace = FileManager.default.temporaryDirectory.appendingPathComponent("a2a-\(token).jsonl")
        traceURL = trace
        child.executableURL = executable
        child.arguments = pipelineArguments(source: source, trace: trace)
        child.standardInput = control
        child.standardOutput = stdout
        child.standardError = stderr
        stdout.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let data = handle.availableData
            if data.isEmpty { handle.readabilityHandler = nil; return }
            Task { @MainActor in
                self?.acceptPipelineOutput(data, token: token)
            }
        }
        stderr.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let data = handle.availableData
            if data.isEmpty { handle.readabilityHandler = nil; return }
            // Only operational errors are shown. stderr never carries captions.
            Task { @MainActor in
                guard let self, self.generation == token, !self.stopping else { return }
                let message = String(decoding: data.suffix(8_192), as: UTF8.self)
                if message.contains("denied") || message.contains("assets not installed") || message.contains("stalled")
                    || message.contains("Audio capture fell behind") || message.contains("Translated audio could not keep up")
                    || message.contains("voice worker failed") || message.contains("voice worker stopped")
                    || message.contains("Hy-SimulMT worker failed")
                    || (message.contains("voice profile") && message.contains("failed")) {
                    self.error = message.trimmingCharacters(in: .whitespacesAndNewlines)
                }
            }
        }
        child.terminationHandler = { [weak self] finished in
            let code = finished.terminationStatus
            Task { @MainActor in
                guard let self, self.generation == token else { return }
                if code != 0, !self.stopping, self.error == nil {
                    self.error = "Translation stopped (\(code)). Check audio recording permission and the translation server, then restart."
                }
                let restart = self.restartAfterSpeakerReset
                self.restartAfterSpeakerReset = false
                self.cleanUp(keepPicture: restart)
                if restart {
                    self.status = "Resetting speakers…"
                    self.launchPipeline()
                    if self.process == nil {
                        self.status = "Translation unavailable"
                        self.sessionPictureDelayMs = 0
                        Task { await self.stopPicture() }
                    }
                }
            }
        }
        process = child
        outputPipe = stdout
        errorPipe = stderr
        controlPipe = control
        do {
            try child.run()
            try? control.fileHandleForReading.close()
            running = true
            status = "Preparing translation…"
        } catch {
            self.error = String(describing: error)
            if child.isRunning { child.terminate() }
            cleanUp()
        }
    }

    func pipelineArguments(source: AudioSource, trace: URL) -> [String] {
        var voice = ["--tts", "clone", "--clone-model", "qwen", "--clone-mode", "xvector"]
        if !reduceVoiceMemory { voice.append("--dual-clone") }
        if let cloneWorker { voice += ["--clone-python", cloneWorker.python, "--clone-worker", cloneWorker.script] }
        if !multipleSpeakers, let voiceReferenceURL { voice += ["--voice-ref", voiceReferenceURL.path] }
        var translation = ["--mt", "hy", "--hy-server", hyServer]
        if hyOnThisMac {
            translation = ["--mt", "hy-local"]
            if let repository, let hyLocalModel {
                translation += ["--hy-worker", HyLocalSetup.worker(root: repository).path, "--hy-model", hyLocalModel.path]
            }
        }
        var arguments = source.arguments + ["--asr", "audio"] + translation + ["--direction", direction.rawValue] + voice + [
            "--translation-only", "--fluent", "--no-shorten", "--speakers", multipleSpeakers ? "auto" : "single", "--playback", playbackMode.rawValue, "--control-stdin",
            "--events-stdout", "--trace", trace.path, "--parent-pid", "\(getpid())"]
        if qualityFirst { arguments.append("--quality-first") }
        // With a delayed picture the lag limit counts from each phrase's slot: a phrase more than
        // 2 s off its picture is skipped rather than pushing the following ones late too.
        else { arguments.append(contentsOf: ["--adaptive-prebuffer", "--max-debt-seconds", "8", "--max-source-lag-seconds",
                                             sessionPictureDelayMs > 0 ? "2" : "20"]) }
        if sessionPictureDelayMs > 0 { arguments.append(contentsOf: ["--presentation-delay-ms", "\(sessionPictureDelayMs)"]) }
        if keepBackground { arguments.append("--background") }
        return arguments
    }

    private func sendPlaybackMode() {
        guard let process, process.isRunning, !stopping, let controlPipe else { return }
        do {
            try controlPipe.fileHandleForWriting.write(contentsOf: Data((playbackMode.rawValue + "\n").utf8))
        } catch {
            self.error = "Could not change playback. Stop and restart the session."
        }
    }

    func stop() {
        restartAfterSpeakerReset = false
        guard let process, !stopping else { return }
        stopping = true
        status = "Stopping…"
        process.terminate()
        let token = generation
        Task {
            try? await Task.sleep(for: .seconds(3))
            if generation == token, process.isRunning { kill(process.processIdentifier, SIGKILL) }
        }
    }

    /// Start a fresh helper and fresh resident voice workers; no old speaker profile or
    /// in-flight model response can leak into the new session. The picture window remains.
    func resetSpeakers() {
        guard running, !stopping, process?.isRunning == true else { return }
        voiceReferenceURL = nil
        voiceLearning.removeAll()
        error = nil
        stop()
        restartAfterSpeakerReset = true
        status = "Resetting speakers…"
    }

    private func cleanUp(keepPicture: Bool = false) {
        outputPipe?.fileHandleForReading.readabilityHandler = nil
        errorPipe?.fileHandleForReading.readabilityHandler = nil
        try? controlPipe?.fileHandleForWriting.close()
        controlPipe = nil
        outputPipe = nil
        errorPipe = nil
        process = nil
        running = false
        stopping = false
        status = error == nil ? "Ready" : "Translation unavailable"
        captions.removeAll()
        voiceLearning.removeAll()
        refreshVoiceMemoryCount()
        pending.removeAll()
        if let traceURL { try? FileManager.default.removeItem(at: traceURL) }
        traceURL = nil
        if !keepPicture {
            sessionPictureDelayMs = 0
            if mirror != nil || pictureWindow != nil { Task { await stopPicture() } }
        }
    }

    private func consume(_ data: Data) {
        pending.append(data)
        guard pending.count <= 1_048_576 else {
            error = "The translation helper exceeded its message limit."
            stop()
            return
        }
        while let newline = pending.firstIndex(of: 10) {
            let line = pending.prefix(upTo: newline)
            if let event = try? JSONSerialization.jsonObject(with: line) as? [String: Any] {
                if let state = event["voice_status"] as? String, ["collecting", "preparing", "ready"].contains(state),
                   let speaker = event["speaker"] as? String, speaker.utf8.count <= 16,
                   voiceLearning[speaker] != nil || voiceLearning.count < 8 {
                    let seconds = (event["seconds"] as? NSNumber)?.doubleValue ?? 0
                    voiceLearning[speaker] = VoiceLearningProgress(state: state, seconds: seconds.isFinite ? min(15, max(0, seconds)) : 0,
                                                                  reused: event["reused"] as? Bool ?? false)
                } else if let kind = event["caption"] as? String, let text = event["text"] as? String {
                    status = "Translating captions"
                    if !kind.hasPrefix("source."), let id = event["id"] as? String {
                        if let index = captions.firstIndex(where: { $0.id == id }) {
                            captions[index].text = String(text.prefix(4_000))
                        } else {
                            captions.append(CaptionLine(id: id, text: String(text.prefix(4_000))))
                        }
                        if captions.count > 100 { captions.removeFirst(captions.count - 100) }
                    }
                } else if event["ev"] as? String == "error", let message = event["message"] as? String {
                    error = message
                } else if event["ev"] as? String == "state.change", let to = event["to"] as? String {
                    // Translation-only sessions are silent until the voice is ready and while catching up.
                    if to.hasPrefix("acquiring_voice") {
                        status = "Learning the speakers’ voices…"
                    } else if to.hasPrefix("translating") {
                        status = "Translating"
                    } else if to == "degraded" {
                        status = "Catching up…"
                    }
                }
            }
            pending.removeSubrange(...newline)
        }
    }

    private func acceptPipelineOutput(_ data: Data, token: UUID) {
        guard generation == token, !stopping else { return }
        consume(data)
    }

    /// Exercise the same output gate in the synthetic UI lifecycle test.
    func simulateVoiceStatusForSmoke(_ speaker: String) {
        guard CommandLine.arguments.contains("--smoke-output") else { return }
        let event: [String: Any] = ["voice_status": "ready", "speaker": speaker, "seconds": 3]
        guard var data = try? JSONSerialization.data(withJSONObject: event) else { return }
        data.append(10)
        acceptPipelineOutput(data, token: generation)
    }
}
