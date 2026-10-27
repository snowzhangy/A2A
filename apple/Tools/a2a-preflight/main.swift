// a2a-preflight: T0.3 runtime checks on this Mac. Prints JSON evidence; never downloads
// assets unless --install-speech-assets is passed explicitly (setup path, D8).
//
//   a2a-preflight [--json out.json] [--translate-probe] [--install-speech-assets]

import A2AAudio
import CoreAudio
import Foundation
import Metal
import Speech
import Translation

struct Check: Encodable {
    let id: String
    let status: String          // pass | fail | pending | info
    let detail: String
    var data: [String: String] = [:]
}

var checks: [Check] = []
@MainActor func record(_ id: String, _ status: String, _ detail: String, _ data: [String: String] = [:]) {
    checks.append(Check(id: id, status: status, detail: detail, data: data))
    FileHandle.standardError.write(Data("[\(status)] \(id): \(detail)\n".utf8))
}

let args = CommandLine.arguments
let jsonOut = args.firstIndex(of: "--json").map { args[$0 + 1] }
let translateProbe = args.contains("--translate-probe")
let installSpeech = args.contains("--install-speech-assets")

// MARK: Host

func sysctlString(_ name: String) -> String {
    var size = 0
    sysctlbyname(name, nil, &size, nil, 0)
    var buf = [UInt8](repeating: 0, count: max(1, size))
    sysctlbyname(name, &buf, &size, nil, 0)
    return String(decoding: buf.prefix { $0 != 0 }, as: UTF8.self)
}

func sysctlInt(_ name: String) -> Int64 {
    var v: Int64 = 0
    var size = MemoryLayout<Int64>.size
    sysctlbyname(name, &v, &size, nil, 0)
    return v
}

let os = ProcessInfo.processInfo.operatingSystemVersion
record("host", "info", "\(sysctlString("machdep.cpu.brand_string")), \(sysctlInt("hw.memsize") >> 30) GB",
       ["chip": sysctlString("machdep.cpu.brand_string"),
        "ram_gb": String(sysctlInt("hw.memsize") >> 30),
        "os": "\(os.majorVersion).\(os.minorVersion).\(os.patchVersion)",
        "os_build": sysctlString("kern.osversion"),
        "model": sysctlString("hw.model"),
        "perf_cores": String(sysctlInt("hw.perflevel0.physicalcpu")),
        "eff_cores": String(sysctlInt("hw.perflevel1.physicalcpu"))])

if let device = MTLCreateSystemDefaultDevice() {
    record("metal.device", "pass", device.name,
           ["unified_memory": String(device.hasUnifiedMemory),
            "recommended_max_working_set_gb": String(format: "%.1f", Double(device.recommendedMaxWorkingSetSize) / 1e9)])
} else {
    record("metal.device", "fail", "no Metal device")
}
record("ane.placement", "pending", "Actual ANE/GPU placement needs per-model profiling (Instruments/powermetrics); not inferable from loading")

// MARK: Speech (SpeechAnalyzer / SpeechTranscriber)

let zh = Locale(identifier: "zh-CN")
record("speech.transcriber.available", SpeechTranscriber.isAvailable ? "pass" : "fail",
       "SpeechTranscriber.isAvailable = \(SpeechTranscriber.isAvailable)")
let supported = await SpeechTranscriber.supportedLocales
let zhEquivalent = await SpeechTranscriber.supportedLocale(equivalentTo: zh)
let installed = await SpeechTranscriber.installedLocales
let zhLike = supported.filter { $0.identifier.lowercased().hasPrefix("zh") }.map(\.identifier).sorted()
record("speech.transcriber.zh_supported", zhEquivalent != nil ? "pass" : "fail",
       "supportedLocale(equivalentTo: zh-CN) = \(zhEquivalent?.identifier ?? "nil")",
       ["zh_locales": zhLike.joined(separator: ","), "supported_count": String(supported.count)])
let zhInstalled = installed.contains { $0.identifier.lowercased().hasPrefix("zh") }
record("speech.transcriber.zh_installed", zhInstalled ? "pass" : "pending",
       zhInstalled ? "zh asset installed" : "zh asset not installed; setup path must install it",
       ["installed": installed.map(\.identifier).sorted().joined(separator: ",")])
if let zhEquivalent {
    let module = SpeechTranscriber(locale: zhEquivalent, preset: .timeIndexedProgressiveTranscription)
    let status = await AssetInventory.status(forModules: [module])
    record("speech.asset_status", status == .installed ? "pass" : "pending", "AssetInventory.status = \(status)")
    if let format = await SpeechAnalyzer.bestAvailableAudioFormat(compatibleWith: [module]) {
        record("speech.audio_format", "info", "\(format)",
               ["sample_rate": String(format.sampleRate), "channels": String(format.channelCount)])
    }
    if installSpeech, status != .installed {
        do {
            if let request = try await AssetInventory.assetInstallationRequest(supporting: [module]) {
                try await request.downloadAndInstall()
                record("speech.asset_install", "pass", "installed via AssetInventory")
            }
        } catch {
            record("speech.asset_install", "fail", "\(error)")
        }
    }
}

// MARK: Translation

let availability = LanguageAvailability()
let zhHans = Locale.Language(identifier: "zh-Hans")
let en = Locale.Language(identifier: "en")
let mtStatus = await availability.status(from: zhHans, to: en)
record("translation.zh_en_status", mtStatus == .installed ? "pass" : (mtStatus == .supported ? "pending" : "fail"),
       "LanguageAvailability zh-Hans→en = \(mtStatus)")
if translateProbe {
    let session = TranslationSession(installedSource: zhHans, target: en)
    let probe = "我们下周开会。"
    let t0 = Date()
    do {
        let response = try await session.translate(probe)
        record("translation.installed_session_probe", "pass",
               "translated in \(Int(Date().timeIntervalSince(t0) * 1000)) ms (first call, includes load)",
               ["target": response.targetText])
        let t1 = Date()
        _ = try await session.translate("这个方案我们不同意。")
        record("translation.warm_latency", "info", "\(Int(Date().timeIntervalSince(t1) * 1000)) ms for one clause")
    } catch {
        record("translation.installed_session_probe", mtStatus == .installed ? "fail" : "info",
               "TranslationSession(installedSource:) with status \(mtStatus) threw: \(error)")
    }
}

// MARK: Core Audio

do {
    let processes = try AudioProcessList.current()
    record("coreaudio.process_list", "pass", "\(processes.count) audio process objects readable",
           ["running_output": String(processes.filter(\.isRunningOutput).count)])
} catch {
    record("coreaudio.process_list", "fail", "\(error)")
}
do {
    let output = try AudioDevice.defaultOutput()
    record("coreaudio.default_output", "info", output.name,
           ["uid": output.uid, "nominal_rate": String(output.nominalSampleRate),
            "latency_frames": String(output.latencyFrames(scope: kAudioObjectPropertyScopeOutput)),
            "safety_offset_frames": String(output.safetyOffsetFrames(scope: kAudioObjectPropertyScopeOutput)),
            "buffer_frames": String(output.bufferFrameSize),
            "transport": output.transportTypeName])
} catch {
    record("coreaudio.default_output", "fail", "\(error)")
}
record("coreaudio.tap_mute_lifecycle", "pending",
       "Runtime tap/mute and crash restoration need capture permission on the interactive runner (a2a-tapctl lifecycle)")

// MARK: Toolchain

let env = ProcessInfo.processInfo.environment
record("toolchain", "info", env["DEVELOPER_DIR"] ?? "default xcode-select",
       ["swift": env["A2A_SWIFT_VERSION"] ?? "see scripts/check.sh output"])

if let jsonOut {
    let encoder = JSONEncoder()
    encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
    try encoder.encode(checks).write(to: URL(fileURLWithPath: jsonOut))
}
let failed = checks.filter { $0.status == "fail" }.count
FileHandle.standardError.write(Data("\(checks.count) checks, \(failed) failed\n".utf8))
