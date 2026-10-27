import A2ACore
import Testing

@Suite struct TermMapTests {
    @Test func chineseTermsMatchInsideWords() {
        let map = TermMap(["全真": "Quanzhen", "全真教": "the Quanzhen school"])
        #expect(map.apply("他是全真教的弟子") == "他是the Quanzhen school的弟子")
        #expect(map.apply("全真七子") == "Quanzhen七子")
    }

    @Test func latinTermsOnlyMatchWholeWords() {
        let map = TermMap(["Li": "Lee"], ignoresCase: true)
        #expect(map.apply("Lily met li, LI.") == "Lily met Lee, Lee.")
    }

    @Test func replacementsAreNotRescanned() {
        let map = TermMap(["Quanzhen": "Chwahn-jen", "jen": "X"], ignoresCase: true)
        #expect(map.apply("The quanzhen Taoists") == "The Chwahn-jen Taoists")
    }

    @Test func emptyTermsAreIgnored() {
        let map = TermMap([" ": "x", "": "y"])
        #expect(map.isEmpty)
        #expect(map.apply("unchanged") == "unchanged")
    }
}
