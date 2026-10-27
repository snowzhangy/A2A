// Casting for the system-voice plumbing synthesizer: each tracked speaker gets a distinct
// installed voice of a plausible gender for their pitch. Pure: the synthesizer lists the
// installed voices and keeps the cast.

public struct VoiceCasting: Sendable {
    public enum Gender: String, Sendable { case female, male }

    public struct Candidate: Sendable, Equatable {
        public let identifier: String
        public let name: String
        public let language: String
        public let gender: Gender
        /// Lower is better: quality, then voice family, then exact locale.
        public let rank: Int

        public init(identifier: String, name: String, language: String, gender: Gender, rank: Int) {
            self.identifier = identifier
            self.name = name
            self.language = language
            self.gender = gender
            self.rank = rank
        }
    }

    public struct Role: Sendable, Equatable {
        public let identifier: String
        /// Distinguishes speakers who share a voice once a pool runs out.
        public let pitchMultiplier: Float
    }

    /// Installed voice as reported by the system.
    public struct InstalledVoice: Sendable {
        public let identifier: String
        public let name: String
        public let language: String
        /// nil when the system does not say.
        public let gender: Gender?
        /// 1 default, 2 enhanced, 3 premium.
        public let quality: Int
        public let novelty: Bool
        public let personal: Bool

        public init(identifier: String, name: String, language: String, gender: Gender?, quality: Int,
                    novelty: Bool, personal: Bool) {
            self.identifier = identifier
            self.name = name
            self.language = language
            self.gender = gender
            self.quality = quality
            self.novelty = novelty
            self.personal = personal
        }
    }

    /// Pitch below this suggests a male voice, above it a female one. Speech in the band
    /// around it goes to the gender cast less so far, to keep speakers apart.
    static let splitHz = 180.0
    static let ambiguousHz = 165.0...195.0
    static let sharedPitch: [Float] = [0.88, 1.12, 0.8, 1.2]

    /// The voice families the system reports without a gender.
    static let knownGender: [String: Gender] = [
        "Eddy": .male, "Reed": .male, "Rocko": .male, "Grandpa": .male,
        "Flo": .female, "Sandy": .female, "Shelley": .female, "Grandma": .female,
        "Albert": .male, "Fred": .male, "Junior": .male, "Ralph": .male, "Kathy": .female,
    ]

    public let candidates: [Candidate]
    public private(set) var roles: [Role] = []
    private var genders: [Gender] = []

    public init(candidates: [Candidate]) {
        self.candidates = candidates.sorted { ($0.rank, $0.name, $0.identifier) < ($1.rank, $1.name, $1.identifier) }
    }

    /// Usable voices for a target locale such as "en-US" or "zh-CN". Novelty voices and the
    /// user's Personal Voice never voice other people. Mandarin excludes zh-HK (Cantonese).
    public static func candidates(from voices: [InstalledVoice], locale: String) -> [Candidate] {
        let language = locale.prefix(2).lowercased()
        return voices.compactMap { v in
            let lang = String(v.language.map { $0 == "_" ? "-" : $0 })
            guard lang.lowercased().hasPrefix(language), !v.novelty, !v.personal else { return nil }
            if language == "zh", lang.lowercased().hasSuffix("hk") { return nil }
            let family = v.identifier.hasPrefix("com.apple.eloquence.") ? 2
                : v.identifier.hasPrefix("com.apple.speech.synthesis.voice.") ? 1 : 0
            let base = String(v.name.split(separator: " ").first ?? Substring(v.name))
            guard let gender = v.gender ?? knownGender[base] else { return nil }
            let rank = (3 - min(3, max(1, v.quality))) * 100 + family * 10 + (lang == locale ? 0 : 1)
            return Candidate(identifier: v.identifier, name: v.name, language: lang, gender: gender, rank: rank)
        }
    }

    /// Casts the next speaker. Returns the role index (stable for the session).
    public mutating func cast(pitchHz: Double) -> Int? {
        guard !candidates.isEmpty else { return nil }
        let males = genders.filter { $0 == .male }.count
        let females = genders.count - males
        var gender: Gender = pitchHz < Self.splitHz ? .male : .female
        if Self.ambiguousHz.contains(pitchHz), males != females {
            gender = males < females ? .male : .female
        }
        var pool = candidates.filter { $0.gender == gender }
        if pool.isEmpty { pool = candidates }
        let taken = roles.map(\.identifier)
        let role: Role
        if let fresh = pool.first(where: { !taken.contains($0.identifier) }) {
            role = Role(identifier: fresh.identifier, pitchMultiplier: 1)
        } else {
            // Every voice of that gender is cast: reuse the least-used one at another pitch.
            let counts = pool.map { c in taken.filter { $0 == c.identifier }.count }
            let pick = pool[counts.firstIndex(of: counts.min()!)!]
            let shared = counts.min()! - 1
            role = Role(identifier: pick.identifier, pitchMultiplier: Self.sharedPitch[shared % Self.sharedPitch.count])
        }
        roles.append(role)
        genders.append(gender)
        return roles.count - 1
    }
}
