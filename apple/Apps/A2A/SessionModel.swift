import A2AAudio
import A2ACore
import A2AModels
import AppKit
import Darwin
import Observation
import Speech

struct AudioSource: Identifiable {
    let id: String
    let name: String
    let arguments: [String]
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
    var sources: [AudioSource] = []
    var selectedSource = "" { didSet { refreshVoiceMemoryCount() } }
    var sourceCaption = ""
    var captions: [CaptionLine] = []
    var status = "Ready"
    var error: String?
    var running = false
    var stopping = false
    var checking = false
    var languagesReady = false
    var speechReady = false
    var translationReady = false
    var languageDetail = "Check languages to prepare offline translation."
    var direction: TranslationDirection = .chineseToEnglish {
        didSet {
            guard direction != oldValue else { return }
            languagesReady = false
            speechReady = false
            translationReady = false
            languageDetail = "Check languages for \(direction.label)."
            sourceCaption = ""
            captions.removeAll()
            error = nil
        }
    }
    /// Experimental: speak the target language in the source speaker's voice (research-only).
    var originalVoice = false
    var rememberVoices = false
    var rememberedVoiceCount = 0
    private var voiceLearning: [String: VoiceLearningProgress] = [:]
    var voiceLearningDetail: String {
        guard originalVoice, running, !voiceLearning.isEmpty else { return "" }
        return voiceLearning.keys.sorted().compactMap { speaker in
            guard let p = voiceLearning[speaker] else { return nil }
            switch p.state {
            case "ready": return "\(speaker): \(p.reused ? "saved voice ready" : "voice ready")"
            case "preparing": return "\(speaker): preparing \(p.reused ? "saved voice" : "voice")"
            default: return "\(speaker): collecting \(String(format: "%.1f", p.seconds))/3.0 s"
            }
        }.joined(separator: " · ")
    }
    var voiceModel = ExperimentalVoiceModel.qwen {
        didSet {
            guard voiceModel != oldValue, !installingVoiceModel else { return }
            voiceSetupDetail = ""
            error = nil
        }
    }
    var voiceInstallations: [ExperimentalVoiceInstallation] = []
    var checkingVoiceModels = false
    var installingVoiceModel = false
    var voiceSetupDetail = "Checking experimental models…"
    var selectedVoiceInstallation: ExperimentalVoiceInstallation? {
        voiceInstallations.first { $0.key == voiceModel.rawValue }
    }
    var voiceReady: Bool { !originalVoice || selectedVoiceInstallation?.installed == true }
    var playbackMode: PlaybackMode = .translated {
        didSet {
            guard playbackMode != oldValue else { return }
            sendPlaybackMode()
        }
    }
    let cloneWorker = SessionModel.findCloneWorker()
    private var process: Process?
    private var outputPipe: Pipe?
    private var errorPipe: Pipe?
    private var controlPipe: Pipe?
    private var pending = Data()
    private var generation = UUID()
    private var traceURL: URL?
    private var languageCheck = UUID()

    private var voiceMemoryDirectory: URL? {
        guard !selectedSource.isEmpty, let cloneWorker else { return nil }
        let root = URL(fileURLWithPath: cloneWorker.script).deletingLastPathComponent().deletingLastPathComponent()
            .appendingPathComponent(".local/voice-profiles", isDirectory: true)
        let source = selectedSource.hasPrefix("pid:") ? sources.first { $0.id == selectedSource }?.name ?? selectedSource : selectedSource
        return VoiceMemoryStore.directory(root: root, source: source)
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
        guard let cloneWorker, !installingVoiceModel else {
            voiceSetupDetail = "Prepare the research environment with uv sync --project research --locked."
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

    func installVoiceModel() async {
        guard !running, !installingVoiceModel, let cloneWorker else { return }
        let selected = voiceModel
        installingVoiceModel = true
        voiceSetupDetail = "Installing \(selected.name)…"
        error = nil
        defer { installingVoiceModel = false }
        do {
            let script = URL(fileURLWithPath: cloneWorker.script).deletingLastPathComponent()
                .appendingPathComponent("voice_models.py").path
            voiceInstallations = try await ExperimentalVoiceSetup.run(python: cloneWorker.python, script: script, install: selected)
            voiceSetupDetail = "\(selected.name) installed."
        } catch { self.error = String(describing: error); voiceSetupDetail = "Model installation failed. You can retry to resume." }
    }

    func refreshSources() async {
        do {
            let snapshot = try await Task.detached { try AudioProcessList.current() }.value
            var seen: Set<String> = []
            sources = snapshot.filter { $0.pid != getpid() && !$0.bundleID.hasPrefix("dev.a2a.") }
                .compactMap { p in
                    let bundle = p.appBundleRoot.flatMap { Bundle(path: $0)?.bundleIdentifier } ?? p.bundleID
                    let id = bundle.isEmpty ? "pid:\(p.pid)" : bundle
                    guard seen.insert(id).inserted else { return nil }
                    let name = p.appBundleRoot.map { URL(fileURLWithPath: $0).deletingPathExtension().lastPathComponent }
                        ?? NSRunningApplication(processIdentifier: p.pid)?.localizedName
                        ?? URL(fileURLWithPath: p.executablePath).lastPathComponent
                    return AudioSource(id: id, name: name.isEmpty ? id : name,
                                       arguments: bundle.isEmpty ? ["--tap-pid", "\(p.pid)"] : ["--tap-bundle", bundle])
                }.sorted { $0.name.localizedCaseInsensitiveCompare($1.name) == .orderedAscending }
            if !sources.contains(where: { $0.id == selectedSource }) {
                selectedSource = sources.first(where: { $0.id == "us.zoom.xos" })?.id ?? sources.first?.id ?? ""
            }
        } catch { self.error = String(describing: error) }
    }

    func checkLanguages() async {
        let direction = self.direction
        let token = UUID()
        languageCheck = token
        checking = true
        defer { if languageCheck == token { checking = false } }
        let speechInstalled: Bool
        let translationInstalled: Bool
        do {
            _ = try await AppleSpeechRecognizer.checkInstalled(locale: Locale(identifier: direction.source.speechLocaleIdentifier))
            speechInstalled = true
        } catch { speechInstalled = false }
        do {
            try await AppleTranslator.checkInstalled(source: .init(identifier: direction.source.translationIdentifier),
                                                     target: .init(identifier: direction.target.translationIdentifier))
            translationInstalled = true
        } catch { translationInstalled = false }
        guard languageCheck == token, direction == self.direction else { return }
        speechReady = speechInstalled
        translationReady = translationInstalled
        languagesReady = speechReady && translationReady
        languageDetail = languagesReady ? "\(direction.source.name) speech and \(direction.label) translation are installed. Processing stays on this Mac."
            : "\(speechReady ? "\(direction.source.name) speech is installed." : "Install \(direction.source.name) speech.") \(translationReady ? "Translation is installed." : "Install \(direction.label) translation.")"
    }

    // Explicit setup action; session startup never initiates downloads.
    func installSpeech() async {
        let direction = self.direction
        checking = true
        defer { checking = false }
        do {
            guard let locale = await SpeechTranscriber.supportedLocale(equivalentTo: Locale(identifier: direction.source.speechLocaleIdentifier)) else {
                throw AdapterError.unsupported("\(direction.source.name) speech on this Mac")
            }
            _ = try await AssetInventory.reserve(locale: locale)
            let module = SpeechTranscriber(locale: locale, preset: .timeIndexedProgressiveTranscription)
            if let request = try await AssetInventory.assetInstallationRequest(supporting: [module]) {
                try await request.downloadAndInstall()
            }
            await checkLanguages()
        } catch { self.error = String(describing: error) }
    }

    /// The development app lives in the repository's .local/apps; its research worker sits above it.
    static func findCloneWorker() -> (python: String, script: String)? {
        var dir = Bundle.main.bundleURL.deletingLastPathComponent()
        while dir.path != "/" {
            let script = dir.appendingPathComponent("research/tts_worker.py").path
            let python = dir.appendingPathComponent("research/.venv/bin/python").path
            if FileManager.default.fileExists(atPath: script) {
                return FileManager.default.isExecutableFile(atPath: python) ? (python, script) : nil
            }
            dir = dir.deletingLastPathComponent()
        }
        return nil
    }

    func start() {
        guard process == nil, languagesReady, voiceReady, !installingVoiceModel,
              let source = sources.first(where: { $0.id == selectedSource }) else { return }
        let executable = Bundle.main.bundleURL.appendingPathComponent("Contents/Helpers/a2a-pipeline")
        guard FileManager.default.isExecutableFile(atPath: executable.path) else {
            error = "The translation helper is missing. Build the app with scripts/build_app.sh."
            return
        }
        error = nil
        captions.removeAll()
        voiceLearning.removeAll()
        sourceCaption = ""
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
        var voice = ["--tts", "system"]
        if originalVoice, let cloneWorker {
            voice = ["--tts", "clone", "--clone-model", voiceModel.rawValue,
                     "--clone-python", cloneWorker.python, "--clone-worker", cloneWorker.script]
            if rememberVoices, let directory = voiceMemoryDirectory { voice += ["--voice-memory", directory.path] }
        }
        child.arguments = source.arguments + ["--asr", "apple", "--mt", "apple", "--direction", direction.rawValue] + voice + [
            "--translation-only", "--fluent", "--speakers", "auto", "--playback", playbackMode.rawValue, "--control-stdin",
            "--events-stdout", "--trace", trace.path, "--parent-pid", "\(getpid())"]
        child.standardInput = control
        child.standardOutput = stdout
        child.standardError = stderr
        stdout.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let data = handle.availableData
            if data.isEmpty { handle.readabilityHandler = nil; return }
            Task { @MainActor in
                guard let self, self.generation == token else { return }
                self.consume(data)
            }
        }
        stderr.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let data = handle.availableData
            if data.isEmpty { handle.readabilityHandler = nil; return }
            // Only operational errors are shown. stderr never carries captions.
            Task { @MainActor in
                guard let self, self.generation == token else { return }
                let message = String(decoding: data.suffix(8_192), as: UTF8.self)
                if message.contains("denied") || message.contains("assets not installed") || message.contains("stalled")
                    || message.contains("voice worker failed") || message.contains("voice worker stopped")
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
                    self.error = "Translation stopped (\(code)). Check audio recording permission and installed languages, then restart."
                }
                self.cleanUp()
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
            cleanUp()
        }
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
        guard let process else { return }
        stopping = true
        status = "Stopping…"
        process.terminate()
        let token = generation
        Task {
            try? await Task.sleep(for: .seconds(3))
            if generation == token, process.isRunning { kill(process.processIdentifier, SIGKILL) }
        }
    }

    private func cleanUp() {
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
        sourceCaption = ""
        captions.removeAll()
        voiceLearning.removeAll()
        refreshVoiceMemoryCount()
        pending.removeAll()
        if let traceURL { try? FileManager.default.removeItem(at: traceURL) }
        traceURL = nil
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
                   voiceLearning[speaker] != nil || voiceLearning.count < 4 {
                    let seconds = (event["seconds"] as? NSNumber)?.doubleValue ?? 0
                    voiceLearning[speaker] = VoiceLearningProgress(state: state, seconds: seconds.isFinite ? min(15, max(0, seconds)) : 0,
                                                                  reused: event["reused"] as? Bool ?? false)
                } else if let kind = event["caption"] as? String, let text = event["text"] as? String {
                    status = "Translating captions"
                    if kind.hasPrefix("source.") { sourceCaption = String(text.prefix(2_000)) }
                    else if let id = event["id"] as? String {
                        captions.append(CaptionLine(id: id, text: String(text.prefix(4_000))))
                        if captions.count > 100 { captions.removeFirst(captions.count - 100) }
                    }
                } else if event["ev"] as? String == "error", let message = event["message"] as? String {
                    error = message
                } else if event["ev"] as? String == "state.change", let to = event["to"] as? String {
                    // Translation-only sessions are silent until the voice is ready and while catching up.
                    if to.hasPrefix("acquiring_voice") {
                        status = originalVoice ? "Learning the speakers’ voices…" : "Preparing voices…"
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
}
