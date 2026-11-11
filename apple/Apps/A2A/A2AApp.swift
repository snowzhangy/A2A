import AppKit
import A2ACore
import A2AMedia
import A2AModels
import SwiftUI
import UniformTypeIdentifiers

@MainActor final class AppDelegate: NSObject, NSApplicationDelegate {
    var model: SessionModel?
    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        guard let model else { return .terminateNow }
        if model.settingUp { model.cancelSetup() }
        guard model.running else { return .terminateNow }
        model.stop()
        Task {
            while model.running { try? await Task.sleep(for: .milliseconds(50)) }
            NSApp.reply(toApplicationShouldTerminate: true)
        }
        return .terminateLater
    }
}

@main struct A2AApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var delegate
    @State private var model = SessionModel()

    var body: some Scene {
        Window("A2A", id: "main") {
            ContentView(model: model).onAppear { delegate.model = model }
        }
        .defaultSize(width: 780, height: 700)
        .commands { CommandGroup(replacing: .newItem) {} }
    }
}

private struct ContentView: View {
    @Bindable var model: SessionModel

    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            HStack {
                Text("A2A").font(.largeTitle.bold())
                Picker("Language", selection: $model.direction) {
                    ForEach(TranslationDirection.allCases, id: \.self) { direction in
                        Text(direction.label).tag(direction)
                    }
                }
                .labelsHidden().fixedSize()
                .disabled(model.running || model.checking || model.settingUp)
                .accessibilityIdentifier("translation-direction")
                Spacer()
                Label(model.status, systemImage: model.running ? "waveform" : "circle")
                    .foregroundStyle(model.running ? Color.green : Color.secondary)
                    .accessibilityIdentifier("session-status")
            }

            GroupBox("Translation setup") {
                VStack(alignment: .leading, spacing: 10) {
                    HStack {
                        Text("Hy-SimulMT")
                        Picker("Translation location", selection: $model.hyOnThisMac) {
                            Text("On this Mac").tag(true)
                            Text("Server").tag(false)
                        }
                        .pickerStyle(.segmented).labelsHidden().fixedSize()
                        .disabled(model.running || model.checking || model.settingUp)
                        .accessibilityIdentifier("hy-location")
                        Spacer()
                        Button("Check") { Task { await model.checkLanguages(); await model.refreshVoiceModels() } }
                            .disabled(model.running || model.checking || model.settingUp)
                        if model.checking { ProgressView().controlSize(.small) }
                    }
                    if !model.hyOnThisMac {
                        TextField("Server address", text: $model.hyServer)
                            .disabled(model.running || model.checking || model.settingUp)
                    }
                    Text(model.languageDetail).font(.callout).foregroundStyle(.secondary)
                    HStack {
                        Text("Qwen3-TTS voice")
                        if model.voiceReady {
                            Label("Ready", systemImage: "checkmark.circle").foregroundStyle(.green)
                        } else { Text("Not ready").foregroundStyle(.secondary) }
                        if model.checkingVoiceModels { ProgressView().controlSize(.small) }
                        Spacer()
                    }
                    if !model.voiceSetupDetail.isEmpty {
                        Text(model.voiceSetupDetail).font(.callout).foregroundStyle(.secondary)
                    }
                    HStack {
                        Button(model.settingUp ? "Setting up…" : "Set up models") { Task { await model.setupModels() } }
                            .disabled(model.running || model.settingUp || model.repository == nil)
                        if model.settingUp { ProgressView().controlSize(.small) }
                        else if !model.voiceReady || (model.hyOnThisMac && !model.languagesReady) {
                                Text(model.hyOnThisMac ? "Downloads about 22 GB of models." : "Downloads about 2 GB of models.")
                                    .font(.callout).foregroundStyle(.secondary)
                        }
                    }
                    if model.settingUp { Button("Cancel setup") { model.cancelSetup() } }
                    if !model.setupDetail.isEmpty { Text(model.setupDetail).font(.callout).foregroundStyle(.secondary) }
                }.frame(maxWidth: .infinity, alignment: .leading).padding(6)
            }

            Text("A2A translates the sound playing on this Mac, including other apps.")
                .font(.callout).foregroundStyle(.secondary)
            HStack {
                Text("Speakers")
                Picker("Speakers", selection: $model.multipleSpeakers) {
                    Text("One").tag(false)
                    Text("Multiple").tag(true)
                }
                .pickerStyle(.segmented).labelsHidden().fixedSize()
                .disabled(model.running || model.stopping || model.settingUp)
                .help("Choose One for a single speaker to keep their voice stable. Multiple tracks speakers separately.")
                .accessibilityIdentifier("speaker-mode")
            }
            HStack(spacing: 14) {
                Button(model.running ? "Stop" : "Start translation") {
                    if model.running { model.stop() } else { model.start() }
                }
                .buttonStyle(.borderedProminent)
                .disabled(model.stopping || model.choosingPicture || model.settingUp || (!model.running && (!model.languageReadyForSession || !model.voiceReady)))
                .accessibilityIdentifier("session-toggle")
                Picker("Listen to", selection: $model.playbackMode) {
                    Text("Original").tag(PlaybackMode.original)
                    Text("Translated").tag(PlaybackMode.translated)
                }
                .pickerStyle(.segmented).frame(maxWidth: 260).disabled(model.stopping)
                .accessibilityIdentifier("playback-mode")
            }

            DisclosureGroup("Playback settings") {
                VStack(alignment: .leading, spacing: 10) {
                    Toggle("Prioritize complete phrases", isOn: $model.qualityFirst)
                        .help("Waits for each phrase to finish generating before playback and allows more lag.")
                        .disabled(model.running || model.stopping)
                    Toggle("Keep music and effects", isOn: $model.keepBackground)
                        .disabled(model.running || model.stopping)
                    Toggle("Use less memory for voices", isOn: $model.reduceVoiceMemory)
                        .help("Uses one Qwen worker instead of two. Speech may fall behind during rapid dialogue.")
                        .disabled(model.running || model.stopping)
                    if !model.multipleSpeakers {
                        HStack {
                            Text(model.voiceReferenceURL?.lastPathComponent ?? "Voice reference: automatic")
                                .lineLimit(1)
                            Button("Choose WAV…") {
                                let panel = NSOpenPanel()
                                panel.allowedContentTypes = [UTType(filenameExtension: "wav")!]
                                panel.allowsMultipleSelection = false
                                panel.canChooseDirectories = false
                                if panel.runModal() == .OK { model.voiceReferenceURL = panel.url }
                            }
                            if model.voiceReferenceURL != nil { Button("Clear") { model.voiceReferenceURL = nil } }
                        }
                        .disabled(model.running || model.stopping)
                        .help("Optional: choose 1–15 seconds of clean speech from the same speaker. Voice matching is not guaranteed.")
                    }
                    Toggle("Match picture to translated speech", isOn: $model.matchPicture)
                        .help("Opens a delayed copy of the video window. Protected video may appear black.")
                        .disabled(model.running || model.stopping || model.choosingPicture)
                    if model.matchPicture {
                        HStack {
                            Stepper(value: $model.pictureDelaySeconds, in: 3...20, step: 1) {
                                Text("Picture delay: \(Int(model.pictureDelaySeconds)) s")
                            }.disabled(model.running || model.choosingPicture)
                            Text("Fine-tune")
                            Slider(value: $model.pictureOffsetMs, in: -500...500, step: 20).frame(maxWidth: 150)
                            Text("\(Int(model.pictureOffsetMs)) ms").monospacedDigit().frame(width: 64)
                        }.font(.callout)
                    }
                    if model.rememberedVoiceCount > 0 {
                        Button("Forget previously saved voices") { model.forgetVoices() }.disabled(model.running)
                    }
                }.disabled(model.stopping)
            }

            if model.running {
                HStack {
                    Text(model.voiceLearningDetail.isEmpty ? "Learning speakers…" : model.voiceLearningDetail)
                        .font(.callout).foregroundStyle(.secondary)
                        .accessibilityIdentifier("voice-learning-progress")
                    Spacer()
                    Button("Reset") { model.resetSpeakers() }
                        .disabled(model.stopping)
                        .help("Restarts translation and learns all speakers again from the current audio. A selected WAV reference is cleared.")
                        .accessibilityIdentifier("reset-speakers")
                }
            }
            if let error = model.error {
                Text(error).font(.callout).foregroundStyle(.red).textSelection(.enabled)
            }

            ScrollViewReader { proxy in
                ScrollView {
                    VStack(alignment: .leading, spacing: 14) {
                        if model.captions.isEmpty {
                            ContentUnavailableView("Live captions", systemImage: "captions.bubble",
                                                   description: Text("Translations appear here when you start."))
                        }
                        ForEach(model.captions) { caption in
                            Text(caption.text).font(.title2).textSelection(.enabled)
                                .frame(maxWidth: .infinity, alignment: .leading).id(caption.id)
                        }
                    }.padding()
                }.onChange(of: model.captions.last?.id) {
                    if let id = model.captions.last?.id { proxy.scrollTo(id, anchor: .bottom) }
                }
            }.background(.quaternary.opacity(0.3), in: RoundedRectangle(cornerRadius: 12))
            Text("Voice matching is experimental. Use it only with speakers who consented. Longer translations may finish after their picture.")
                .font(.caption).foregroundStyle(.secondary)
        }
        .padding(24).frame(minWidth: 680, minHeight: 620)
        .task {
            let args = CommandLine.arguments
            if let i = args.firstIndex(of: "--smoke-output"), i + 1 < args.count {
                let initialDirection = model.direction.rawValue
                model.voiceInstallations = [ExperimentalVoiceInstallation(key: "qwen", installed: true, bytes: 1_000)]
                let qwenReady = model.selectedVoiceInstallation?.installed == true
                let voiceGate = model.voiceReady == (model.cloneWorker != nil)
                let launchArgs = model.pipelineArguments(source: .systemAudio, trace: URL(fileURLWithPath: "/tmp/a2a-ui-smoke-trace"))
                let directPipeline = launchArgs.contains("audio") && (launchArgs.contains("hy") || launchArgs.contains("hy-local"))
                    && launchArgs.contains("qwen") && launchArgs.contains("xvector") && launchArgs.contains("--no-shorten")
                    && launchArgs.contains("--dual-clone") == !model.reduceVoiceMemory
                    && !launchArgs.contains("--qwen-cloud") && !launchArgs.contains("system")
                let initialSpeakers = model.multipleSpeakers
                model.multipleSpeakers = false
                let singleArgs = model.pipelineArguments(source: .systemAudio, trace: URL(fileURLWithPath: "/tmp/a2a-ui-smoke-trace"))
                model.multipleSpeakers = true
                let multipleArgs = model.pipelineArguments(source: .systemAudio, trace: URL(fileURLWithPath: "/tmp/a2a-ui-smoke-trace"))
                let speakerModesWork = singleArgs.contains("single") && multipleArgs.contains("auto")
                let initialMemoryMode = model.reduceVoiceMemory
                model.reduceVoiceMemory = true
                let lowMemoryArgs = model.pipelineArguments(source: .systemAudio, trace: URL(fileURLWithPath: "/tmp/a2a-ui-smoke-trace"))
                model.reduceVoiceMemory = false
                let standardMemoryArgs = model.pipelineArguments(source: .systemAudio, trace: URL(fileURLWithPath: "/tmp/a2a-ui-smoke-trace"))
                let memoryModesWork = !lowMemoryArgs.contains("--dual-clone") && standardMemoryArgs.contains("--dual-clone")
                model.reduceVoiceMemory = initialMemoryMode
                model.multipleSpeakers = initialSpeakers
                model.multipleSpeakers = false
                model.voiceReferenceURL = URL(fileURLWithPath: args[i + 1]).appendingPathExtension("missing.wav")
                let missingReferenceBlocked = model.preflightFailure()?.contains("voice reference") == true
                let sampleURL = URL(fileURLWithPath: args[i + 1]).appendingPathExtension("reference.wav")
                let shortSample = WAVAudio(sampleRate: 16_000, channels: 1, samples: [Float](repeating: 0, count: 1_600))
                try? WAV.write(shortSample, to: sampleURL)
                model.voiceReferenceURL = sampleURL
                let shortReferenceBlocked = model.preflightFailure()?.contains("between 1 and 15 seconds") == true
                let sample = WAVAudio(sampleRate: 16_000, channels: 1,
                                      samples: (0..<16_000).map { Float(sin(Double($0) * 0.08) * 0.1) })
                try? WAV.write(sample, to: sampleURL)
                model.voiceReferenceURL = sampleURL
                let referenceArgs = model.pipelineArguments(source: .systemAudio, trace: URL(fileURLWithPath: "/tmp/a2a-ui-smoke-trace"))
                let validReferenceAccepted = model.preflightFailure()?.contains("voice reference") != true
                    && referenceArgs.contains("--voice-ref") && referenceArgs.contains(sampleURL.path)
                model.multipleSpeakers = true
                let multipleClearsReference = model.voiceReferenceURL == nil
                    && !model.pipelineArguments(source: .systemAudio, trace: URL(fileURLWithPath: "/tmp/a2a-ui-smoke-trace")).contains("--voice-ref")
                model.voiceReferenceURL = nil
                model.multipleSpeakers = initialSpeakers
                model.languagesReady = true
                model.direction = .englishToChinese
                let directionResetReadiness = !model.languagesReady
                model.languagesReady = true
                model.languageDetail = "Synthetic preview · no audio is captured."
                model.captions = [CaptionLine(id: "preview", text: "我们下周开会。")]
                let initialPlayback = model.playbackMode.rawValue
                model.playbackMode = .original
                let originalSelected = model.playbackMode == .original
                model.playbackMode = .translated
                if args.contains("--smoke-setup") { await model.setupModels(serverOnlyOverride: true) }
                let setupSucceeded = !args.contains("--smoke-setup") || model.setupDetail == "Setup complete."
                var resetRestarted = true, resetClearedReference = true, resetSuppressesStale = true
                var resetKeepsDelay = true, stopCancelsReset = true
                var resetDiagnostics: [String: Any] = [:]
                if args.contains("--smoke-reset") {
                    let initialPicture = model.matchPicture
                    model.matchPicture = false
                    model.multipleSpeakers = false
                    model.languagesReady = true
                    model.voiceReferenceURL = sampleURL
                    model.start()
                    model.simulateVoiceStatusForSmoke("S1")
                    let firstPID = model.pipelineProcessID
                    let hadVoice = !model.voiceLearningDetail.isEmpty
                    model.sessionPictureDelayMs = 8_000
                    model.resetSpeakers()
                    resetClearedReference = model.voiceReferenceURL == nil && model.voiceLearningDetail.isEmpty
                        && !model.pipelineArguments(source: .systemAudio, trace: URL(fileURLWithPath: "/tmp/a2a-ui-smoke-trace")).contains("--voice-ref")
                    model.simulateVoiceStatusForSmoke("S9")
                    resetSuppressesStale = model.stopping && model.voiceLearningDetail.isEmpty
                    resetDiagnostics["first_pid"] = firstPID ?? -1
                    resetDiagnostics["had_voice"] = hadVoice
                    resetDiagnostics["stopping_after_150ms"] = model.stopping
                    resetDiagnostics["voice_after_150ms"] = model.voiceLearningDetail
                    for _ in 0..<100 {
                        if let current = model.pipelineProcessID, current != firstPID, !model.stopping { break }
                        try? await Task.sleep(for: .milliseconds(50))
                    }
                    resetRestarted = hadVoice && firstPID != nil && model.pipelineProcessID != nil
                        && model.pipelineProcessID != firstPID && model.running && !model.stopping
                    resetDiagnostics["second_pid"] = model.pipelineProcessID ?? -1
                    resetDiagnostics["error"] = model.error ?? ""
                    let restartedArgs = model.pipelineArguments(source: .systemAudio, trace: URL(fileURLWithPath: "/tmp/a2a-ui-smoke-trace"))
                    resetKeepsDelay = model.sessionPictureDelayMs == 8_000 && restartedArgs.contains("--presentation-delay-ms")
                    model.resetSpeakers()
                    model.stop() // An explicit Stop while reset is pending must cancel its restart.
                    for _ in 0..<100 where model.running { try? await Task.sleep(for: .milliseconds(50)) }
                    try? await Task.sleep(for: .milliseconds(200))
                    stopCancelsReset = !model.running && model.pipelineProcessID == nil
                    model.matchPicture = initialPicture
                    model.multipleSpeakers = initialSpeakers
                }
                try? FileManager.default.removeItem(at: sampleURL)
                try? await Task.sleep(for: .milliseconds(500))
                let url = URL(fileURLWithPath: args[i + 1])
                let window = NSApp.windows.first(where: { $0.title == "A2A" })
                let evidence: [String: Any] = ["mode": "synthetic-ui", "running": model.running,
                    "window_number": window?.windowNumber ?? 0, "direction_initial": initialDirection,
                    "direction_final": model.direction.rawValue, "direction_reset_readiness": directionResetReadiness,
                    "qwen_ready": qwenReady, "voice_gate": voiceGate, "direct_pipeline": directPipeline,
                    "speaker_modes": speakerModesWork, "memory_modes": memoryModesWork, "missing_reference_blocked": missingReferenceBlocked,
                    "short_reference_blocked": shortReferenceBlocked, "valid_reference_accepted": validReferenceAccepted,
                    "multiple_clears_reference": multipleClearsReference, "setup_succeeded": setupSucceeded,
                    "setup_detail": model.setupDetail, "setup_error": model.error ?? "",
                    "reset_restarted": resetRestarted, "reset_cleared_reference": resetClearedReference,
                    "reset_suppresses_stale": resetSuppressesStale, "reset_keeps_delay": resetKeepsDelay,
                    "stop_cancels_reset": stopCancelsReset, "reset_diagnostics": resetDiagnostics,
                    "playback_initial": initialPlayback, "playback_original_selected": originalSelected,
                    "playback_final": model.playbackMode.rawValue]
                try? JSONSerialization.data(withJSONObject: evidence, options: [.prettyPrinted, .sortedKeys])
                    .write(to: url.deletingPathExtension().appendingPathExtension("json"))
                try? await Task.sleep(for: .seconds(20))
                NSApp.terminate(nil)
            } else {
                model.refreshVoiceMemoryCount()
                await model.checkLanguages()
                await model.refreshVoiceModels()
            }
        }
        .onChange(of: model.direction) {
            if !CommandLine.arguments.contains("--smoke-output") { Task { await model.checkLanguages() } }
        }
    }
}
