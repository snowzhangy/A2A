import AppKit
import A2ACore
import A2AModels
import SwiftUI
import UniformTypeIdentifiers

@MainActor final class AppDelegate: NSObject, NSApplicationDelegate {
    var model: SessionModel?
    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        guard let model, model.running else { return .terminateNow }
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
        .defaultSize(width: 820, height: 780)
        .commands { CommandGroup(replacing: .newItem) {} }
    }
}

private struct ContentView: View {
    @Bindable var model: SessionModel

    var body: some View {
        VStack(alignment: .leading, spacing: 20) {
            HStack(alignment: .firstTextBaseline) {
                Text("A2A").font(.largeTitle.bold())
                Picker("Direction", selection: $model.direction) {
                    ForEach(TranslationDirection.allCases, id: \.self) { direction in
                        Text(direction.label).tag(direction)
                    }
                }
                .labelsHidden()
                .fixedSize()
                .disabled(model.running || model.checking)
                .accessibilityIdentifier("translation-direction")
                Spacer()
                Label(model.status, systemImage: model.running ? "waveform" : "circle")
                    .foregroundStyle(model.running ? Color.green : Color.secondary)
                    .accessibilityIdentifier("session-status")
            }
            Picker("Translation backend", selection: $model.backend) {
                Text("Local: Hy-SimulMT + voice model").tag(TranslationBackend.local)
                Text("Cloud: Qwen LiveTranslate").tag(TranslationBackend.qwenCloud)
            }.disabled(model.running || model.checking)
            GroupBox(model.backend == .local ? "Hy-SimulMT" : "Qwen LiveTranslate") {
                VStack(alignment: .leading, spacing: 10) {
                  if model.backend == .local {
                    Picker("Hy-SimulMT", selection: $model.hyOnThisMac) {
                        Text("On this Mac").tag(true)
                        Text("Server").tag(false)
                    }
                    .pickerStyle(.segmented)
                    .labelsHidden()
                    .fixedSize()
                    .disabled(model.running || model.checking)
                    .accessibilityIdentifier("hy-location")
                    if !model.hyOnThisMac {
                        TextField("Server address", text: $model.hyServer).disabled(model.running || model.checking)
                    }
                    Text(model.languageDetail).font(.callout).foregroundStyle(.secondary)
                    HStack {
                        Button(model.hyOnThisMac ? "Check again" : "Check server") { Task { await model.checkLanguages() } }
                        if model.checking { ProgressView().controlSize(.small) }
                    }.disabled(model.checking || model.running)
                  } else {
                    SecureField("QwenCloud API key", text: $model.cloudAPIKey)
                        .disabled(model.running).textContentType(.password)
                    Picker("API key region", selection: $model.cloudRegion) {
                        Text("Singapore").tag("ap-southeast-1")
                        Text("Beijing").tag("cn-beijing")
                    }.disabled(model.running)
                    Text("Selected app audio is sent to Qwen Cloud; usage charges may apply. The key stays in memory for this app session.")
                        .font(.callout).foregroundStyle(.secondary)
                    Text("Speaker cloning is provided by Qwen; a default voice may be heard before cloning is ready. Music and effects are unavailable in this mode.")
                        .font(.callout).foregroundStyle(.secondary)
                  }
                }.frame(maxWidth: .infinity, alignment: .leading).padding(6)
            }
            // No app to choose: the tap hears the machine's whole output mix, so whatever starts playing is
            // translated. Our own playback is excluded, so the clone never answers itself.
            Label("Listens to everything this Mac plays. Sound we produce is left out of what we hear.",
                  systemImage: "speaker.wave.2")
                .font(.callout).foregroundStyle(.secondary).padding(.horizontal, 6)
            HStack {
                Button(model.running ? "Stop" : "Start translation") {
                    if model.running { model.stop() } else { model.start() }
                }
                .buttonStyle(.borderedProminent)
                .disabled(model.stopping || model.choosingPicture || (!model.running && (!model.languageReadyForSession || !model.voiceReady
                    || model.installingVoiceModel)))
                .accessibilityIdentifier("session-toggle")
                Text(model.playbackMode == .original
                     ? "Original audio. \(model.direction.target.name) captions continue."
                     : "Translated audio. The original is muted.")
                    .font(.callout).foregroundStyle(.secondary)
            }
            HStack {
                Text("Listen to")
                Picker("Playback", selection: $model.playbackMode) {
                    Text("Original").tag(PlaybackMode.original)
                    Text("Translated").tag(PlaybackMode.translated)
                }
                .pickerStyle(.segmented)
                .labelsHidden()
                .frame(maxWidth: 300)
                .disabled(model.stopping)
                .accessibilityIdentifier("playback-mode")
                .help("Switch audio during a session. Translation and captions keep running.")
            }
            if model.backend == .local {
            DisclosureGroup("Names and terminology") {
                VStack(alignment: .leading, spacing: 8) {
                    termFileRow("Pronunciation", selection: $model.pronunciationURL,
                                help: "Choose a JSON object mapping translated words to how they should be spoken.")
                    Text("Changes apply to the next session. Pronunciation changes speech only.")
                        .font(.caption).foregroundStyle(.secondary)
                }.disabled(model.running || model.stopping)
            }
            Toggle("Prioritize complete phrases", isOn: $model.qualityFirst)
                .disabled(model.running || model.stopping)
                .help("Allows more delay to reduce skipped or cut-off speech. Applies to the next session.")
                .accessibilityIdentifier("quality-first")
            Toggle("Keep music and effects (experimental)", isOn: $model.keepBackground)
                .disabled(model.running || model.stopping)
                .help("Separates background sound from dialogue locally. Separation can leave artifacts or traces of voices.")
                .accessibilityIdentifier("keep-background")
            Toggle("Match picture to translated speech (opens a delayed copy of the video window)", isOn: $model.matchPicture)
                .disabled(model.running || model.stopping || model.choosingPicture || model.backend != .local)
                .help("Start asks which window shows the video. A2A shows that window late and gives translated speech, original audio, and music and effects the same delay, so each phrase lands on its picture. Protected (DRM) video captures as black. Keep the source window visible: some players stop drawing hidden windows.")
                .accessibilityIdentifier("match-picture")
            if model.matchPicture && model.backend == .local {
                HStack(spacing: 16) {
                    Stepper(value: $model.pictureDelaySeconds, in: 3...20, step: 1) {
                        Text("Picture delay \(Int(model.pictureDelaySeconds)) s")
                    }
                    .disabled(model.running || model.stopping || model.choosingPicture)
                    .help("Longer delays let slower phrases land on time. Applies to the next session.")
                    .accessibilityIdentifier("picture-delay")
                    Text("Fine-tune")
                    Slider(value: $model.pictureOffsetMs, in: -500...500, step: 20)
                        .frame(maxWidth: 180)
                        .help("Moves the picture only, during a session. Positive shows it later. Use it once per output device.")
                        .accessibilityIdentifier("picture-offset")
                    Text("\(Int(model.pictureOffsetMs)) ms").monospacedDigit().frame(width: 64, alignment: .trailing)
                }.font(.callout)
            }
            Toggle("Speak \(model.direction.target.name) in each original speaker’s voice (experimental, research-only model)",
                   isOn: $model.originalVoice)
                .disabled(model.running || model.installingVoiceModel || model.cloneWorker == nil)
                .help(model.cloneWorker == nil ? "Run `uv sync --project research --locked` and fetch the voice model first."
                      : "Learns each speaker’s voice from the first few seconds they speak. Identity is not yet qualified.")
                .accessibilityIdentifier("original-voice")
            if model.originalVoice {
                VStack(alignment: .leading, spacing: 6) {
                    HStack {
                        Picker("Voice model", selection: $model.voiceModel) {
                            ForEach(ExperimentalVoiceModel.allCases, id: \.self) { voice in
                                Text(voice.name).tag(voice)
                            }
                        }
                        .fixedSize()
                        .disabled(model.running || model.installingVoiceModel)
                        .accessibilityIdentifier("voice-model")
                        if model.selectedVoiceInstallation?.installed == true {
                            Label("Installed", systemImage: "checkmark.circle").foregroundStyle(.green)
                        } else if !model.checkingVoiceModels, !model.installingVoiceModel {
                            Button("Install model") { Task { await model.installVoiceModel() } }
                                .disabled(model.running || model.cloneWorker == nil)
                                .accessibilityIdentifier("install-voice-model")
                            if let bytes = model.selectedVoiceInstallation?.bytes {
                                Text(ByteCountFormatter.string(fromByteCount: bytes, countStyle: .file))
                                    .foregroundStyle(.secondary)
                            }
                        }
                        if model.checkingVoiceModels || model.installingVoiceModel { ProgressView().controlSize(.small) }
                        Button("Refresh") { Task { await model.refreshVoiceModels() } }
                            .disabled(model.running || model.checkingVoiceModels || model.installingVoiceModel)
                    }
                    Text(model.voiceModel.playbackDetail)
                        .foregroundStyle(.secondary)
                    if model.voiceModel == .audio8 || model.voiceModel == .omnivoice {
                        Button("Install speech for voice references") { Task { await model.installReferenceSpeech() } }
                            .disabled(model.running || model.checking)
                    }
                    if !model.voiceSetupDetail.isEmpty { Text(model.voiceSetupDetail).foregroundStyle(.secondary) }
                    if model.rememberedVoiceCount > 0 {
                        HStack {
                            Text("\(model.rememberedVoiceCount) previously saved voices").foregroundStyle(.secondary)
                            Button("Forget saved voices") { model.forgetVoices() }.disabled(model.running)
                        }
                    }
                    if !model.voiceLearningDetail.isEmpty {
                        Text(model.voiceLearningDetail).foregroundStyle(.secondary)
                            .accessibilityIdentifier("voice-learning-progress")
                    }
                }.font(.callout)
            }
            }
            if let error = model.error {
                Text(error).font(.callout).foregroundStyle(.red).textSelection(.enabled)
            }
            ScrollViewReader { proxy in
                ScrollView {
                    VStack(alignment: .leading, spacing: 16) {
                        if model.captions.isEmpty {
                            ContentUnavailableView("Live \(model.direction.target.name) captions", systemImage: "captions.bubble",
                                                   description: Text(model.backend == .local ? "Start a session to translate the selected app’s audio locally." : "Start a session to translate the selected app’s audio with Qwen Cloud."))
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
            Text(model.backend == .qwenCloud
                 ? "Development build · Qwen Cloud voice cloning is experimental. Use only with speakers who consented."
                 : model.originalVoice
                 ? "Development build · Experimental voice cloning (research-only model; identity not qualified). Use only with speakers who consented."
                 : "Development build · \(model.direction.target.name) uses system voices, one per detected speaker.")
                .font(.caption).foregroundStyle(.secondary)
            Button("Open audio recording permissions") {
                if let url = URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy_AudioCapture") {
                    NSWorkspace.shared.open(url)
                }
            }.buttonStyle(.link).font(.caption)
        }
        .padding(24).frame(minWidth: 720, minHeight: 700)
        .task {
            // Native view-only screenshot for the local packaging smoke command.
            let args = CommandLine.arguments
            if let i = args.firstIndex(of: "--smoke-output"), i + 1 < args.count {
                // Synthetic presentation data; this path does no capture or inference.
                let initialDirection = model.direction.rawValue
                model.voiceInstallations = ExperimentalVoiceModel.allCases.map {
                    ExperimentalVoiceInstallation(key: $0.rawValue, installed: $0 == .qwen, bytes: 1_000)
                }
                model.originalVoice = true
                let qwenReady = model.voiceReady
                model.voiceModel = .audio8
                let audio8Blocked = !model.voiceReady
                let audio8Args = model.pipelineArguments(source: AudioSource.systemAudio, trace: URL(fileURLWithPath: "/tmp/a2a-ui-smoke-trace"))
                model.voiceModel = .omnivoice
                let omniBlocked = !model.voiceReady
                model.voiceModel = .chatterbox
                let chatterboxBlocked = !model.voiceReady
                let chatterboxArgs = model.pipelineArguments(source: AudioSource.systemAudio, trace: URL(fileURLWithPath: "/tmp/a2a-ui-smoke-trace"))
                model.voiceModel = .qwen
                let launchArgs = model.pipelineArguments(source: AudioSource.systemAudio, trace: URL(fileURLWithPath: "/tmp/a2a-ui-smoke-trace"))
                let directPipeline = launchArgs.contains("audio") && (launchArgs.contains("hy") || launchArgs.contains("hy-local")) && !launchArgs.contains("apple")
                    && (model.cloneWorker == nil || (launchArgs.contains("xvector") && launchArgs.contains("--dual-clone")))
                model.backend = .qwenCloud
                let cloudMissingKeyBlocked = !model.languageReadyForSession
                model.cloudAPIKey = "synthetic-test-key"
                let cloudReady = model.languageReadyForSession && model.voiceReady
                let cloudArgs = model.pipelineArguments(source: AudioSource.systemAudio, trace: URL(fileURLWithPath: "/tmp/a2a-ui-smoke-trace"))
                let cloudIsolated = cloudArgs.contains("--qwen-cloud") && !cloudArgs.contains("synthetic-test-key") && !cloudArgs.contains("--background")
                model.cloudAPIKey = ""
                model.backend = .local
                model.voiceSetupDetail = "Synthetic model preview."
                model.languagesReady = true
                model.direction = .englishToChinese
                let directionResetReadiness = !model.languagesReady
                model.languagesReady = true
                model.languageDetail = "Synthetic UI preview · No capture or inference is running."
                model.captions = [CaptionLine(id: "preview", text: "我们下周开会。")]
                let initialPlayback = model.playbackMode.rawValue
                model.playbackMode = .original
                let originalSelected = model.playbackMode == .original
                model.playbackMode = .translated
                // A missing term file must stop before the helper can take over audio.
                model.pronunciationURL = URL(fileURLWithPath: args[i + 1]).appendingPathExtension("missing")
                model.start()
                let badTermsBlocked = !model.running && model.error != nil
                model.pronunciationURL = nil
                model.error = nil
                try? await Task.sleep(for: .milliseconds(500))
                let url = URL(fileURLWithPath: args[i + 1])
                let window = NSApp.windows.first(where: { $0.title == "A2A" })
                let evidence: [String: Any] = ["mode": "synthetic-ui", "languages_ready": model.languagesReady,
                    "source": "system-audio", "status": model.status,
                    "running": model.running, "window_number": window?.windowNumber ?? 0,
                    "direction_initial": initialDirection, "direction_final": model.direction.rawValue,
                    "direction_reset_readiness": directionResetReadiness,
                    "qwen_ready": qwenReady, "direct_pipeline": directPipeline,
                    "cloud_missing_key_blocked": cloudMissingKeyBlocked, "cloud_ready": cloudReady, "cloud_isolated": cloudIsolated,
                    "audio8_blocked": audio8Blocked, "omni_blocked": omniBlocked, "chatterbox_blocked": chatterboxBlocked,
                    "audio8_reference_helper": audio8Args.contains("--reference-transcriber"),
                    "chatterbox_audio_only": chatterboxArgs.contains("xvector") && !chatterboxArgs.contains("--reference-transcriber"),
                    "voice_model_final": model.voiceModel.rawValue,
                    "missing_terms_blocked": badTermsBlocked,
                    "playback_initial": initialPlayback, "playback_original_selected": originalSelected,
                    "playback_final": model.playbackMode.rawValue]
                try? JSONSerialization.data(withJSONObject: evidence, options: [.prettyPrinted, .sortedKeys])
                    .write(to: url.deletingPathExtension().appendingPathExtension("json"))
                // The shell smoke tool captures this window through WindowServer. AppKit's
                // cacheDisplay cannot capture SwiftUI's composited layers faithfully.
                try? await Task.sleep(for: .seconds(20))
                NSApp.terminate(nil)
            } else {
                model.refreshVoiceMemoryCount()
                await model.checkLanguages()
                await model.refreshVoiceModels()
            }
        }
        .onChange(of: model.direction) {
            // Smoke mode uses synthetic readiness; ordinary changes check the selected assets.
            if !CommandLine.arguments.contains("--smoke-output") {
                Task { await model.checkLanguages() }
            }
        }
    }

    private func termFileRow(_ title: String, selection: Binding<URL?>, help: String) -> some View {
        HStack {
            Text(title).frame(width: 130, alignment: .leading)
            Text(selection.wrappedValue?.lastPathComponent ?? "None").foregroundStyle(.secondary)
                .lineLimit(1)
            Spacer()
            Button("Choose…") {
                let panel = NSOpenPanel()
                panel.allowedContentTypes = [.json]
                panel.allowsMultipleSelection = false
                panel.canChooseDirectories = false
                if panel.runModal() == .OK { selection.wrappedValue = panel.url }
            }
            if selection.wrappedValue != nil {
                Button("Clear") { selection.wrappedValue = nil }
            }
        }.help(help)
    }
}
