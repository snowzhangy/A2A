"""Port of apple/Tests/A2ACoreTests/TermMapTests.swift."""

from a2a_runtime.core.term_map import TermMap


def test_chinese_terms_match_inside_words():
    terms = TermMap({"全真": "Quanzhen", "全真教": "the Quanzhen school"})
    assert terms.apply("他是全真教的弟子") == "他是the Quanzhen school的弟子"
    assert terms.apply("全真七子") == "Quanzhen七子"


def test_latin_terms_only_match_whole_words():
    terms = TermMap({"Li": "Lee"}, ignores_case=True)
    assert terms.apply("Lily met li, LI.") == "Lily met Lee, Lee."


def test_replacements_are_not_rescanned():
    terms = TermMap({"Quanzhen": "Chwahn-jen", "jen": "X"}, ignores_case=True)
    assert terms.apply("The quanzhen Taoists") == "The Chwahn-jen Taoists"


def test_empty_terms_are_ignored():
    terms = TermMap({" ": "x", "": "y"})
    assert terms.is_empty
    assert terms.apply("unchanged") == "unchanged"
