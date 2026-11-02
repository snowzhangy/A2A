// DSP speaker tracker (section 4.2 SpeakerTracker): labels each stretch of speech with a
// speaker so every voice in a dialogue gets its own synthesized voice. No model: speakers
// are told apart by the mel cepstrum of their voiced frames (A2AMedia.Voiceprint).
//
// Segments are bounded by VAD and capped at a few seconds. A segment joins the nearest
// speaker unless it is clearly different. Creating a speaker needs strong evidence: a very
// large distance with half a second of voicing, or a large distance confirmed within 15 s
// by another unmatched segment, with their combined print still far from every speaker.
// Short replies too brief for that evidence can join a close known speaker, never create one.
// A few speakers are tracked at once. One heard only briefly and not for half a minute gives up
// its slot to the next new voice, so music or effects at the start cannot hold a slot for the
// whole session; speaker IDs are never reused. The tracker prefers merging, because one actor
// alternating between voices is worse than two actors sharing one. Measured on a drama excerpt,
// synthetic two-voice dialogues, and a single-narrator audiobook (reports/week2/speakers.md):
// different-gender speakers separate reliably; similar voices of the same gender often merge.

import A2ACore
import A2AMedia

public struct VoiceprintSpeakerTracker: SpeakerTracker {
    public struct Config: Sendable {
        public var maxSpeakers = 4
        /// Distance (in typical cepstral spreads) beyond which a segment may be a new speaker.
        public var newSpeakerDistance = 0.5
        /// Distance that creates a speaker without confirmation.
        public var immediateDistance = 0.8
        /// A confirmed speaker's combined print must stay this far from every existing one:
        /// two outliers of one voice average back toward it.
        public var confirmedDistance = 0.45
        /// A new speaker speaks again soon: an unmatched segment older than this (canonical
        /// samples) cannot be confirmed, so isolated outliers of one voice never pair up.
        public var candidateSamples: Int64 = 15 * canonicalSampleRate
        /// Voiced 10 ms frames needed to label a segment at all.
        public var minVoicedFrames = 40
        /// Short replies can reuse a confidently matching known speaker without creating one.
        public var minContinuationFrames = 12
        /// Voiced frames needed for a confirmed or immediate new speaker.
        public var minNewSpeakerFrames = 80
        public var minImmediateFrames = 50
        /// Long speech is labeled in pieces of at most this many canonical samples.
        public var maxSegmentSamples: Int64 = 3 * canonicalSampleRate
        /// A speaker's print follows their recent speech: older frames are down-weighted.
        public var clusterFrames = 600
        /// With every slot taken, a speaker heard in fewer than this many segments and not for
        /// `staleSamples` gives its slot to a new one: a burst of music or effects at the start
        /// must not hold a slot the rest of the session (0 never frees a slot).
        public var staleSegments = 4
        public var staleSamples: Int64 = 30 * canonicalSampleRate

        public init() {}
    }

    public let config: Config
    private var analyzer = VoiceprintAnalyzer()
    private struct Cluster {
        let id: SpeakerID
        var print: Voiceprint
        var segments = 1
        var lastEnd: Int64
    }
    private var speakers: [Cluster] = []
    /// Speaker numbers are never reused: a freed slot's ID may still name a prepared voice.
    private var created = 0
    /// An unmatched segment waiting for a second one before it becomes a speaker, and where it ended.
    private var candidate: Voiceprint?
    private var candidateEnd: Int64 = 0
    private var segmentStart: Int64?
    private var segment: Voiceprint?

    public init(config: Config = .init()) {
        self.config = config
    }

    public var speakerCount: Int { speakers.count }
    public func voiceprint(of speaker: SpeakerID) -> Voiceprint? {
        speakers.first { $0.id == speaker }?.print
    }

    /// Median voiced pitch of a speaker so far, for casting a plumbing voice.
    public func pitchHz(of speaker: SpeakerID) -> Double? {
        speakers.first { $0.id == speaker }?.print.medianPitchHz
    }

    public mutating func process(_ frame: AudioFrame16k, vad: [VADEvent]) -> [SpeakerEvent] {
        var events: [SpeakerEvent] = []
        for event in vad {
            switch event {
            case .speechStart(let s):
                if segmentStart == nil { open(at: s) }
            case .speechEnd(let s):
                events += close(at: max(s, segmentStart ?? s))
            }
        }
        analyzer.process(frame.samples, into: &segment)
        if let start = segmentStart, frame.span.end - start >= config.maxSegmentSamples {
            events += close(at: frame.span.end)
            open(at: frame.span.end)
        }
        return events
    }

    private mutating func open(at sample: Int64) {
        segmentStart = sample
        segment = Voiceprint()
    }

    private mutating func close(at end: Int64) -> [SpeakerEvent] {
        defer { segmentStart = nil; segment = nil }
        guard let start = segmentStart, let print = segment, end > start else { return [] }
        let speaker: SpeakerID
        if print.voicedFrames < config.minVoicedFrames {
            guard print.voicedFrames >= config.minContinuationFrames,
                  let known = speakers.min(by: { $0.print.distance(to: print) < $1.print.distance(to: print) }),
                  known.print.distance(to: print) <= config.newSpeakerDistance else { return [] }
            speaker = known.id
            heard(speaker, end: end)
        } else {
            guard let identified = classify(print, start: start, end: end) else { return [] }
            speaker = identified
        }
        return [.assign(speaker, SourceSpan(start: start, end: end))]
    }

    private mutating func classify(_ print: Voiceprint, start: Int64, end: Int64) -> SpeakerID? {
        guard !speakers.isEmpty else { return add(print, end: end) }
        if candidate != nil, start - candidateEnd > config.candidateSamples { candidate = nil }
        var nearest = 0
        var distance = Double.infinity
        for (i, s) in speakers.enumerated() {
            let d = s.print.distance(to: print)
            if d < distance { (nearest, distance) = (i, d) }
        }
        let frames = print.voicedFrames
        let room = speakers.count < config.maxSpeakers || staleSlot(at: start) != nil
        if distance <= config.newSpeakerDistance {
            speakers[nearest].print.add(print)
            speakers[nearest].print.cap(frames: config.clusterFrames)
        } else if room, distance > config.immediateDistance, frames >= config.minImmediateFrames {
            candidate = nil
            return add(print, end: end)
        } else if frames >= config.minNewSpeakerFrames {
            if room, var c = candidate, c.distance(to: print) < distance {
                c.add(print)
                if speakers.allSatisfy({ $0.print.distance(to: c) > config.confirmedDistance }) {
                    candidate = nil
                    return add(c, end: end)
                }
            }
            candidate = print
            candidateEnd = end
        }
        let id = speakers[nearest].id
        heard(id, end: end)
        return id
    }

    private mutating func heard(_ speaker: SpeakerID, end: Int64) {
        guard let i = speakers.firstIndex(where: { $0.id == speaker }) else { return }
        speakers[i].segments += 1
        speakers[i].lastEnd = max(speakers[i].lastEnd, end)
    }

    /// The slot a new speaker may take when all are in use: the least-heard stale speaker.
    private func staleSlot(at sample: Int64) -> Int? {
        speakers.indices
            .filter { speakers[$0].segments < config.staleSegments && sample - speakers[$0].lastEnd > config.staleSamples }
            .min { (speakers[$0].segments, speakers[$0].lastEnd) < (speakers[$1].segments, speakers[$1].lastEnd) }
    }

    private mutating func add(_ print: Voiceprint, end: Int64) -> SpeakerID {
        if speakers.count >= config.maxSpeakers, let slot = staleSlot(at: end) { speakers.remove(at: slot) }
        created += 1
        let id = SpeakerID("S\(created)")
        var p = print
        p.cap(frames: config.clusterFrames)
        speakers.append(Cluster(id: id, print: p, lastEnd: end))
        return id
    }
}
