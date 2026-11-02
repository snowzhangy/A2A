@testable import A2AAudio
import A2ARTShim
import CoreAudio
import Testing

private func proc(_ id: AudioObjectID, pid: pid_t, bundle: String, path: String, ppid: pid_t = 1,
                  output: Bool = true) -> AudioProcessInfo {
    AudioProcessInfo(objectID: id, pid: pid, bundleID: bundle, isRunning: output, isRunningOutput: output,
                     isRunningInput: false, executablePath: path, parentPID: ppid)
}

@Suite struct SourceResolverTests {
    let zoom = proc(10, pid: 100, bundle: "us.zoom.xos", path: "/Applications/zoom.us.app/Contents/MacOS/zoom.us")
    let zoomHelper = proc(11, pid: 101, bundle: "us.zoom.CptHost",
                          path: "/Applications/zoom.us.app/Contents/Frameworks/CptHost.app/Contents/MacOS/CptHost", ppid: 100)
    let other = proc(12, pid: 200, bundle: "com.apple.Music", path: "/System/Applications/Music.app/Contents/MacOS/Music")
    let me = proc(13, pid: 300, bundle: "dev.a2a.tapctl", path: "/tmp/a2a-tapctl")

    @Test func includesHelpersInsideTheAppBundle() throws {
        let r = try SourceResolver.resolve(bundleID: "us.zoom.xos", processes: [zoom, zoomHelper, other, me], selfPID: 300)
        #expect(Set(r.objectIDs) == [10, 11])
        #expect(r.appRoot == "/Applications/zoom.us.app")
        #expect(r.members.first { $0.process.pid == 101 }?.reason.hasPrefix("inside") == true)
    }

    @Test func excludesOwnProcess() throws {
        let selfApp = proc(14, pid: 400, bundle: "dev.a2a.app", path: "/Applications/A2A.app/Contents/MacOS/A2A")
        let selfHelper = proc(15, pid: 401, bundle: "dev.a2a.inference",
                              path: "/Applications/A2A.app/Contents/XPCServices/A2AInference.xpc/Contents/MacOS/A2AInference")
        #expect(throws: SourceResolutionError.self) {
            try SourceResolver.resolve(bundleID: "dev.a2a.app", processes: [selfApp, selfHelper], selfPID: 400,
                                       selfBundleRoot: "/Applications/A2A.app")
        }
    }

    @Test func rejectsAmbiguousBundles() {
        let copy = proc(16, pid: 500, bundle: "us.zoom.xos", path: "/Users/x/Downloads/zoom.us.app/Contents/MacOS/zoom.us")
        #expect(throws: SourceResolutionError.self) {
            try SourceResolver.resolve(bundleID: "us.zoom.xos", processes: [zoom, copy], selfPID: 1)
        }
    }

    @Test func missingSourceIsAnError() {
        #expect(throws: SourceResolutionError.self) {
            try SourceResolver.resolve(bundleID: "us.zoom.xos", processes: [other], selfPID: 1)
        }
    }

    @Test func resolvesOuterAppWhenOnlyItsAudioHelperExists() throws {
        let r = try SourceResolver.resolve(bundleID: "us.zoom.xos", processes: [zoomHelper, other], selfPID: 300,
                                          bundleIDAtRoot: { $0 == "/Applications/zoom.us.app" ? "us.zoom.xos" : nil })
        #expect(r.objectIDs == [11])
        #expect(r.appRoot == "/Applications/zoom.us.app")
    }

    @Test func helperOnlyResolutionStillRejectsTwoCopies() {
        let helperCopy = proc(17, pid: 600, bundle: "us.zoom.CptHost",
                              path: "/Users/x/Downloads/zoom.us.app/Contents/MacOS/helper")
        #expect(throws: SourceResolutionError.self) {
            try SourceResolver.resolve(bundleID: "us.zoom.xos", processes: [zoomHelper, helperCopy], selfPID: 300,
                                       bundleIDAtRoot: { _ in "us.zoom.xos" })
        }
    }

    @Test func liveProcessListIsReadable() throws {
        // Reading process objects needs no capture permission.
        let list = try AudioProcessList.current()
        #expect(list.allSatisfy { $0.pid > 0 })
    }

    @Test func systemAudioHearsEverythingButOurOwnOutput() {
        let selfApp = proc(14, pid: 400, bundle: "dev.a2a.app", path: "/Applications/A2A.app/Contents/MacOS/A2A")
        let selfHelper = proc(15, pid: 401, bundle: "dev.a2a.pipeline",
                              path: "/Applications/A2A.app/Contents/Helpers/a2a-pipeline")
        let source = SourceResolver.systemAudio(processes: [zoom, zoomHelper, other, me, selfApp, selfHelper],
                                                selfPID: 400, selfBundleRoot: "/Applications/A2A.app")
        #expect(source.global)
        // A global tap names nothing it includes — that is the point: no app has to be chosen.
        #expect(source.objectIDs.isEmpty)
        #expect(Set(source.excluded.map { $0.process.pid }) == [400, 401])
    }

    @Test func globalTapDescriptionCarriesTheExclusionList() {
        let description = CATapDescription(stereoGlobalTapButExcludeProcesses: [401])
        #expect(description.isMixdown && !description.isMono)
        #expect((description.processes as? [Any])?.count == 1)  // the processes it leaves out
    }

    @Test func onlyANamedSourceCanExit() {
        let dead: (pid_t) -> Bool = { _ in true }
        let alive: (pid_t) -> Bool = { _ in false }
        let app = try! SourceResolver.resolve(bundleID: "us.zoom.xos", processes: [zoom, other], selfPID: 300)
        #expect(app.processesAllExited(liveness: dead))
        #expect(!app.processesAllExited(liveness: alive))
        // The whole mix names no process, so it is never "gone": whoever plays may come and go.
        let mix = SourceResolver.systemAudio(processes: [zoom, other], selfPID: 300, selfBundleRoot: nil)
        #expect(!mix.processesAllExited(liveness: dead))
    }

    @Test func onlyAGlobalTapWithNothingPlayingIsIdle() {
        // A global tap has no callbacks until something plays: a quiet machine is not a stalled capture.
        let quiet = proc(16, pid: 500, bundle: "com.apple.Music", path: "/System/Applications/Music.app/Contents/MacOS/Music",
                         output: false)
        let selfHelper = proc(15, pid: 401, bundle: "dev.a2a.pipeline",
                              path: "/Applications/A2A.app/Contents/Helpers/a2a-pipeline")
        let mix = SourceResolver.systemAudio(processes: [quiet], selfPID: 400, selfBundleRoot: "/Applications/A2A.app")
        #expect(mix.mixIsIdle(processes: [quiet], selfPID: 400, selfBundleRoot: "/Applications/A2A.app"))
        // Our own output does not count: the tap leaves it out, so it can never deliver frames.
        #expect(mix.mixIsIdle(processes: [quiet, selfHelper], selfPID: 400, selfBundleRoot: "/Applications/A2A.app"))
        // Someone else playing with no frames arriving is a stall.
        #expect(!mix.mixIsIdle(processes: [quiet, other], selfPID: 400, selfBundleRoot: "/Applications/A2A.app"))
        // A process tap is never idle in this sense.
        let app = try! SourceResolver.resolve(bundleID: "us.zoom.xos", processes: [zoom, other], selfPID: 300)
        #expect(!app.mixIsIdle(processes: [], selfPID: 300, selfBundleRoot: nil))
    }

    @Test func ownAppRootWalksUpFromTheHelper() {
        // The app's helper runs from Contents/Helpers, where Bundle.main is not the app bundle itself.
        #expect(SourceResolver.ownAppRoot("/Applications/A2A.app/Contents/Helpers/a2a-pipeline") == "/Applications/A2A.app")
        #expect(SourceResolver.ownAppRoot("/Users/me/bin/a2a-pipeline") == nil)
    }
}

@Suite struct IOProcAdapterTests {
    /// Aggregate input: a mono sub-device input followed by a stereo tap. With offset 1 the
    /// engine must capture the tap channels only.
    @Test func gathersTapChannelsAfterSubdeviceInputs() throws {
        var cfg = a2a_engine_config(channels: 2, capture_ring_frames: 1_024, max_delay_frames: 0,
                                    translated_ring_frames: 1_024, max_block_frames: 512, record_capacity: 64,
                                    rendered_ring_frames: 0, fade_frames: 0)
        let e = try #require(a2a_engine_create(&cfg))
        defer { a2a_engine_destroy(e) }
        a2a_engine_set_input_channel_offset(e, 1)
        let frames = 4
        var mic = [Float](repeating: 9, count: frames)
        var tap: [Float] = [1, -1, 2, -2, 3, -3, 4, -4]
        var outData = [Float](repeating: 7, count: frames * 2)
        let input = AudioBufferList.allocate(maximumBuffers: 2)
        defer { free(input.unsafeMutablePointer) }
        let output = AudioBufferList.allocate(maximumBuffers: 1)
        defer { free(output.unsafeMutablePointer) }
        mic.withUnsafeMutableBytes { m in
            tap.withUnsafeMutableBytes { t in
                outData.withUnsafeMutableBytes { o in
                    input[0] = AudioBuffer(mNumberChannels: 1, mDataByteSize: UInt32(m.count), mData: m.baseAddress)
                    input[1] = AudioBuffer(mNumberChannels: 2, mDataByteSize: UInt32(t.count), mData: t.baseAddress)
                    output[0] = AudioBuffer(mNumberChannels: 2, mDataByteSize: UInt32(o.count), mData: o.baseAddress)
                    var ts = AudioTimeStamp()
                    ts.mFlags = [.hostTimeValid]
                    ts.mHostTime = 1
                    _ = a2a_engine_ioproc(0, &ts, input.unsafePointer, &ts, output.unsafeMutablePointer, &ts,
                                          UnsafeMutableRawPointer(e))
                }
            }
        }
        var got = [Float](repeating: 0, count: frames * 2)
        #expect(a2a_ring_read(a2a_engine_capture_ring(e), &got, UInt32(frames)) == UInt32(frames))
        #expect(got == tap)
        // Capture-only by default: output is silent.
        #expect(outData.allSatisfy { $0 == 0 })
    }
}
