// Language choices shared by the app, reducer, and model drivers. No framework I/O.
public enum TranslationLanguage: String, Sendable {
    case mandarin
    case english

    public var name: String { self == .mandarin ? "Mandarin" : "English" }
    public var speechLocaleIdentifier: String { self == .mandarin ? "zh-CN" : "en-US" }
    public var translationIdentifier: String { self == .mandarin ? "zh-Hans" : "en" }
    public var cloneLanguage: String { self == .mandarin ? "chinese" : "english" }
    public var warmUpText: String { self == .mandarin ? "好的，我们开始吧。" : "Okay, let's begin." }
}

public enum TranslationDirection: String, CaseIterable, Hashable, Sendable {
    case chineseToEnglish = "zh-en"
    case englishToChinese = "en-zh"

    public var source: TranslationLanguage { self == .chineseToEnglish ? .mandarin : .english }
    public var target: TranslationLanguage { self == .chineseToEnglish ? .english : .mandarin }
    public var label: String { "\(source.name) → \(target.name)" }
    public var tracePair: String { "\(source.speechLocaleIdentifier)>\(target.speechLocaleIdentifier)" }
}
