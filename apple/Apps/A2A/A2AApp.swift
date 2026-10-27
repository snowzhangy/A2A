import AppKit
import A2ACore
import A2AModels
import SwiftUI
import Translation

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
    @State private var translationSetup: TranslationSession.Configuration?

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
                .disabled(model.running || model.checking || translationSetup != nil)
                .accessibilityIdentifier("translation-direction")
                Spacer()
                Label(model.status, systemImage: model.running ? "waveform" : "circle")
                    .foregroundStyle(model.running ? Color.green : Color.secondary)
                    .accessibilityIdentifier("session-status")
            }
            GroupBox("1. Prepare languages") {
                VStack(alignment: .leading, spacing: 10) {
                    Text(model.languageDetail).font(.callout).foregroundStyle(.secondary)
                    HStack {
                        Button("Check languages") { Task { await model.checkLanguages() } }
                        if !model.speechReady {
                            Button("Install \(model.direction.source.name) speech") { Task { await model.installSpeech() } }
                        }
                        if !model.translationReady {
                            Button("Install translation languages") {
                                translationSetup = TranslationSession.Configuration(
                                    source: .init(identifier: model.direction.source.translationIdentifier),
                                    target: .init(identifier: model.direction.target.translationIdentifier))
                            }
                        }
                        if model.checking { ProgressView().controlSize(.small) }
                    }.disabled(model.checking || model.running || translationSetup != nil)
                }.frame(maxWidth: .infinity, alignment: .leading).padding(6)
            }
            GroupBox("2. Choose an app playing \(model.direction.source.name)") {
                HStack {
                    Picker("Source", selection: $model.selectedSource) {
                        if model.sources.isEmpty { Text("Play audio in an app, then refresh").tag("") }
                        ForEach(model.sources) { source in Text(source.name).tag(source.id) }
                    }.labelsHidden().accessibilityIdentifier("source-picker")
                    Button("Refresh") { Task { await model.refreshSources() } }
                }.disabled(model.running).padding(6)
            }
            HStack {
                Button(model.running ? "Stop" : "Start translation") {
                    if model.running { model.stop() } else { model.start() }
                }
                .buttonStyle(.borderedProminent)
                .disabled(model.stopping || (!model.running && (!model.languagesReady || !model.voiceReady
                    || model.installingVoiceModel || model.selectedSource.isEmpty)))
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
                    Text(model.voiceModel.playbackDetail + " Stop the session to switch models.")
                        .foregroundStyle(.secondary)
                    if !model.voiceSetupDetail.isEmpty { Text(model.voiceSetupDetail).foregroundStyle(.secondary) }
                    HStack {
                        Toggle("Remember voices for this source", isOn: $model.rememberVoices)
                            .disabled(model.running)
                            .help("Keeps short voice references on this Mac and reuses them after a confident speaker match.")
                            .accessibilityIdentifier("remember-voices")
                        Text("\(model.rememberedVoiceCount) saved").foregroundStyle(.secondary)
                        Button("Forget saved voices") { model.forgetVoices() }
                            .disabled(model.running || model.rememberedVoiceCount == 0)
                    }
                    if !model.voiceLearningDetail.isEmpty {
                        Text(model.voiceLearningDetail).foregroundStyle(.secondary)
                            .accessibilityIdentifier("voice-learning-progress")
                    }
                }.font(.callout)
            }
            if let error = model.error {
                Text(error).font(.callout).foregroundStyle(.red).textSelection(.enabled)
            }
            ScrollViewReader { proxy in
                ScrollView {
                    VStack(alignment: .leading, spacing: 16) {
                        if model.captions.isEmpty {
                            ContentUnavailableView("Live \(model.direction.target.name) captions", systemImage: "captions.bubble",
                                                   description: Text("Start a session to translate the selected app’s audio locally."))
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
            if !model.sourceCaption.isEmpty {
                Text(model.sourceCaption).font(.callout).foregroundStyle(.secondary).lineLimit(3).textSelection(.enabled)
            }
            Text(model.originalVoice
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
                model.sources = [AudioSource(id: "synthetic", name: "Synthetic meeting fixture", arguments: [])]
                model.selectedSource = "synthetic"
                let initialDirection = model.direction.rawValue
                model.voiceInstallations = ExperimentalVoiceModel.allCases.map {
                    ExperimentalVoiceInstallation(key: $0.rawValue, installed: $0 == .qwen, bytes: 1_000)
                }
                model.originalVoice = true
                let qwenReady = model.voiceReady
                model.voiceModel = .omnivoice
                let missingOmniBlocked = !model.voiceReady
                model.voiceModel = .chatterbox
                let missingChatterboxBlocked = !model.voiceReady
                model.voiceModel = .qwen
                model.voiceSetupDetail = "Synthetic model selector preview."
                model.languagesReady = true
                model.speechReady = true
                model.translationReady = true
                model.direction = .englishToChinese
                let directionResetReadiness = !model.languagesReady && !model.speechReady && !model.translationReady
                model.languagesReady = true
                model.speechReady = true
                model.translationReady = true
                model.languageDetail = "Synthetic UI preview · No capture or inference is running."
                model.captions = [CaptionLine(id: "preview", text: "我们下周开会。")]
                model.sourceCaption = "We will meet next week."
                let initialPlayback = model.playbackMode.rawValue
                model.playbackMode = .original
                let originalSelected = model.playbackMode == .original
                model.playbackMode = .translated
                try? await Task.sleep(for: .milliseconds(500))
                let url = URL(fileURLWithPath: args[i + 1])
                let window = NSApp.windows.first(where: { $0.title == "A2A" })
                let evidence: [String: Any] = ["mode": "synthetic-ui", "languages_ready": model.languagesReady,
                    "source_count": model.sources.count, "status": model.status,
                    "running": model.running, "window_number": window?.windowNumber ?? 0,
                    "direction_initial": initialDirection, "direction_final": model.direction.rawValue,
                    "direction_reset_readiness": directionResetReadiness,
                    "voice_model_count": ExperimentalVoiceModel.allCases.count,
                    "qwen_ready": qwenReady, "missing_omnivoice_blocked": missingOmniBlocked,
                    "missing_chatterbox_blocked": missingChatterboxBlocked,
                    "voice_model_final": model.voiceModel.rawValue,
                    "playback_initial": initialPlayback, "playback_original_selected": originalSelected,
                    "playback_final": model.playbackMode.rawValue]
                try? JSONSerialization.data(withJSONObject: evidence, options: [.prettyPrinted, .sortedKeys])
                    .write(to: url.deletingPathExtension().appendingPathExtension("json"))
                // The shell smoke tool captures this window through WindowServer. AppKit's
                // cacheDisplay cannot capture SwiftUI's composited layers faithfully.
                try? await Task.sleep(for: .seconds(20))
                NSApp.terminate(nil)
            } else {
                await model.refreshSources()
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
        .translationTask(translationSetup) { session in
            // The framework supplies a non-Sendable session; this setup task is its sole user.
            nonisolated(unsafe) let setupSession = session
            do { try await setupSession.prepareTranslation(); await model.checkLanguages() }
            catch { model.error = String(describing: error) }
            translationSetup = nil
        }
    }
}
