// Spoken length of a translation: what a target may cost in playback time, what is only a
// filler, and the deletion that fits an over-long target into the slot its source audio took.
//
// A segment of source speech is ~3 s. When the translation needs 6 s to speak, the translated
// bus falls further behind every line and fluent playout catches up by skipping lines
// (reports/week2/live-session-recovery.md). Simultaneous interpreters solve this with deletion:
// say the part that fits rather than nothing. This file is the deterministic half of that —
// no model, no text outside the process, and every rule testable in A2ACore.

/// Words and interjections that carry no content when they are the whole translation.
/// Music, laughter and pauses come back from a translator as one of these; the old policy
/// counted them as failed translations, which is a reportable error and a fallback trigger.
public enum FillerLexicon {
    /// Matched case-insensitively on whole words.
    public static let english: Set<String> = [
        "yeah", "yeh", "yeahh", "yuh", "uh", "uhh", "um", "umm", "hmm", "hm", "hum", "mm", "mmm",
        "mhm", "ah", "ahh", "oh", "ohh", "ooh", "oooh", "eh", "ehh", "huh", "hah", "haha",
        "hahaha", "hehe", "hoho", "hihi", "tsk", "pfft", "hmph", "whoa", "wow",
    ]
    /// Single characters; a whole-target Han filler is a run of these.
    public static let mandarinCharacters: Set<Character> = [
        "嗯", "哦", "噢", "啊", "喔", "呃", "唔", "呀", "哎", "哟", "哼", "哈", "嘿", "嘻", "哇", "呐",
    ]
    /// A filler translation is at most this many English words / Han characters.
    public static let maxWords = 3
    public static let maxHanCharacters = 6

    /// True when `text` says nothing but fillers in `language`. Content words anywhere in it
    /// ("oh, that is wrong") make it a real translation.
    public static func isFillerOnly(_ text: String, language: TranslationLanguage) -> Bool {
        let body = text.trimmingWhitespace()
        guard !body.isEmpty else { return false }
        switch language {
        case .mandarin:
            let characters = body.filter { !$0.isWhitespace && !CommitLexicon.punctuation.contains(NormalizedText.key(for: $0)) }
            return !characters.isEmpty && characters.count <= maxHanCharacters && characters.allSatisfy(mandarinCharacters.contains)
        case .english:
            let words = body.split(whereSeparator: \.isWhitespace).map(Self.englishWord)
            return !words.isEmpty && words.count <= maxWords && words.allSatisfy(english.contains)
        }
    }

    /// Lowercased word with surrounding punctuation stripped (A2ACore stays Foundation-free).
    private static func englishWord(_ word: Substring) -> String {
        var s = word.lowercased()
        while let f = s.first, !letterish(f) { s.removeFirst() }
        while let l = s.last, !letterish(l) { s.removeLast() }
        return String(s)
    }

    private static func letterish(_ ch: Character) -> Bool {
        ch.unicodeScalars.contains { $0.properties.isAlphabetic || $0.properties.numericType != nil }
    }
}

/// Cuts a translation down to the audio time its source segment took.
public enum TargetShortener {
    /// Delimiters a shortened target may end at (clause and sentence boundaries).
    static let delimiters: Set<Character> = [",", ";", ":", ".", "!", "?", "，", "。", "！", "？",
                                             "；", "：", "、", "…", "-", "—"]
    /// Trailing punctuation is removed after a cut: the synthesizer needs no ellipsis and a
    /// dangling comma reads as a mistake.
    private static let trimmable: Set<Character> = [",", ";", ":", ".", "!", "?", "，", "。", "！", "？",
                                                    "；", "：", "、", "…", "-", "—", ")", "\"", "”", "’", "'"]

    /// Longest prefix of `text` that fits `maxCharacters`, cut at a clause boundary when that
    /// keeps at least `preferClauseFraction` of the budget and at the deepest word boundary
    /// otherwise. Returns nil when the longest safe prefix is a fragment (less than
    /// `minKeepFraction` of the budget) so the caller rejects the unit instead of speaking a
    /// misleading half-sentence.
    public static func shorten(_ text: String, to maxCharacters: Int,
                              wordSegmenter: (any WordSegmenter)? = nil,
                              preferClauseFraction: Double = 0.75,
                              minKeepFraction: Double = 0.4) -> String? {
        let trimmed = text.trimmingWhitespace()
        guard maxCharacters > 0 else { return nil }
        guard trimmed.count > maxCharacters else { return trimmed.isEmpty ? nil : trimmed }
        let characters = Array(trimmed)
        var clauseCut = 0
        var wordCut = 0
        for (i, ch) in characters.enumerated() where i + 1 <= maxCharacters {
            if delimiters.contains(ch) { clauseCut = max(clauseCut, i + 1) }
            if ch.isWhitespace { wordCut = max(wordCut, i + 1) }
        }
        // Prefer a real clause ending only when it fills most of the slot. An early clause
        // ending left 1.4 s of speech in a 3 s slot (1 November live session: a clause at 40%
        // of the budget beat a word cut near 100%), so a deeper word boundary wins below that.
        var cut = clauseCut >= Int(preferClauseFraction * Double(maxCharacters)) ? clauseCut : max(clauseCut, wordCut)
        if cut == 0 {
            // Han targets have no spaces: fall back to a character boundary the segmenter
            // (when injected) confirms is not inside a word.
            cut = safeHanCut(limit: maxCharacters, text: trimmed, wordSegmenter: wordSegmenter)
        }
        guard cut > 0 else { return nil }
        var kept = Array(characters.prefix(cut))
        while let last = kept.last, trimmable.contains(last) || last.isWhitespace { kept.removeLast() }
        // Never end on a dangling function word: "set the price of" is not a sentence. Han text
        // has no spaces, so there the back-off drops one character at a time.
        for _ in 0..<6 {
            guard endsDangling(kept) else { break }
            if let space = kept.lastIndex(where: \.isWhitespace) { kept = Array(kept[..<space]) }
            else { kept.removeLast() }
            while let last = kept.last, trimmable.contains(last) || last.isWhitespace { kept.removeLast() }
            if kept.isEmpty { return nil }
        }
        let result = String(kept).trimmingWhitespace()
        guard Double(result.count) >= minKeepFraction * Double(maxCharacters), result.count >= 2 else { return nil }
        return result
    }

    /// Largest offset ≤ `limit` that is not strictly inside a segmenter word.
    private static func safeHanCut(limit: Int, text: String, wordSegmenter: (any WordSegmenter)?) -> Int {
        for offset in stride(from: limit, through: max(1, limit - 8), by: -1) {
            guard let segmenter = wordSegmenter else { return offset }
            if !segmenter.splitsWord(in: text, at: offset) { return offset }
        }
        return 0
    }

    /// Words that leave an English sentence unfinished when the cut lands after them.
    static let danglingEnglish: Set<String> = [
        "a", "an", "the", "and", "or", "but", "as", "if", "than", "then", "so", "at", "in", "on",
        "of", "to", "for", "with", "by", "from", "into", "onto", "over", "under", "about",
        "after", "before", "between", "during", "per", "via", "that", "which", "who", "whom",
        "whose", "this", "these", "those", "my", "your", "our", "their", "its", "his", "her",
        "is", "are", "was", "were", "be", "been", "being", "have", "has", "had", "will", "would",
        "can", "could", "should", "may", "might", "must", "not", "very", "more", "most",
    ]

    private static func endsDangling(_ characters: [Character]) -> Bool {
        guard let lastWord = characters.split(whereSeparator: \.isWhitespace).last else { return false }
        var word = String(lastWord).lowercased()
        while let l = word.last, trimmable.contains(l) { word.removeLast() }
        if word.isEmpty { return false }
        if danglingEnglish.contains(word) || CommitLexicon.dangling.contains(word) { return true }
        // Han: a final single-character function word, e.g. "…的成本" cut to "…的".
        guard word.unicodeScalars.allSatisfy({ TranslationValidator.isHanScalar($0) }) else { return false }
        return CommitLexicon.dangling.contains(String(Array(word).suffix(1)))
    }
}
