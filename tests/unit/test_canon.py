"""P2 canon: natural-unit chunking (C-02), one tokeniser for index and query (C-03),
verified-only loading (C-01). No network, no database."""
import pytest

from canon.chunk import ChunkError, clean_lines, han_number, levels_from_config, ref_label, roman, split
from canon.ingest import clean_wikitext
from canon.text import normalize_for_match, search_tokens, tsquery

BOOKS = """*** START OF THE PROJECT GUTENBERG EBOOK X ***
Intro text that is not a passage.

THE FIRST BOOK

I. From my grandfather Verus I learned good morals and the government of
my temper.

II. From the reputation and remembrance of my father, modesty and a manly
character. [1]

THE SECOND BOOK

I. Begin the morning by saying to thyself.
*** END OF THE PROJECT GUTENBERG EBOOK X ***
License text."""

LEVELS = [{"name": "book", "pattern": r"^THE (\w+) BOOK$", "value": "ordinal"},
          {"name": "item", "pattern": r"^([IVXLC]+)\. ", "value": "roman", "inline": True}]


def test_books_and_sections_become_refs_and_labels():
    ps = split(clean_lines(BOOKS), levels_from_config(LEVELS))
    assert [p.ref for p in ps] == ["1.1", "1.2", "2.1"]
    assert ps[0].text == ("From my grandfather Verus I learned good morals and the government "
                          "of my temper.")
    assert "[1]" not in ps[1].text and "License" not in ps[-1].text
    assert ref_label("Suy tưởng, quyển {book}, đoạn {item}", ps[2]) == "Suy tưởng, quyển 2, đoạn 1"


def test_ambiguous_patterns_fail_loudly():
    text = "THE FIRST BOOK\nI. a\nI. b\n"
    with pytest.raises(ChunkError, match="duplicate refs"):
        split(clean_lines(text), levels_from_config(LEVELS))


def test_text_before_the_first_heading_is_an_error_not_a_guess():
    with pytest.raises(ChunkError, match="before any"):
        split(clean_lines("I. orphan section\n"), levels_from_config(LEVELS))


def test_fables_as_a_titled_sequence():
    text = "THE FOX AND THE GRAPES\n\nA hungry fox saw some grapes.\n\nTHE WOLF AND THE LAMB\n\nA wolf met a lamb.\n"
    ps = split(clean_lines(text), levels_from_config([
        {"name": "fable", "pattern": r"^([A-Z][A-Z ,'-]+)$", "value": "sequence", "title_group": 1}]))
    assert [(p.ref, p.title) for p in ps] == [("1", "THE FOX AND THE GRAPES"),
                                             ("2", "THE WOLF AND THE LAMB")]


def test_chinese_chapters_from_wikitext():
    wiki = ("{{header|title=道德經}}\n==第一章==\n道可道，\n非常道。<ref>注</ref>\n"
            "==第二章==\n天下皆知{{另|美|媄}}之爲美。\n")
    ps = split(clean_lines(clean_wikitext(wiki)), levels_from_config(
        [{"name": "ch", "pattern": r"^=+\s*第(.+?)章\s*=+$", "value": "han"}]))
    # no space glued between Chinese lines; a variant template keeps its main reading
    assert [(p.ref, p.text) for p in ps] == [("1", "道可道，非常道。"), ("2", "天下皆知美之爲美。")]


def test_a_lost_section_marker_becomes_an_honest_range():
    text = "THE FIRST BOOK\nI. a\nII. b\nIV. d\n"            # the source lost 'III.'
    ps = split(clean_lines(text), levels_from_config(LEVELS))
    assert [p.ref for p in ps] == ["1.1", "1.2-3", "1.4"]


def test_merged_verses_keep_their_range():
    lv = levels_from_config([{"name": "v", "pattern": r"^(\d+)(?:, (\d+))?\. ", "inline": True,
                              "range_group": 2}])
    assert [p.ref for p in split(["1. a", "2, 3. b", "4. c"], lv)] == ["1", "2-3", "4"]


@pytest.mark.parametrize("s, n", [("一", 1), ("十", 10), ("十二", 12), ("二十", 20),
                                  ("八十一", 81), ("三十三", 33), ("12", 12)])
def test_han_numbers(s, n):
    assert han_number(s) == n


def test_roman_numbers():
    assert [roman(x) for x in ("IV", "IX", "XLIV", "LIII")] == [4, 9, 44, 53]


# ---- one tokeniser for index and query (spec §9) ---------------------------------------
def test_index_and_query_use_the_same_tokens():
    passage = "Kiểm soát cơn giận"
    assert set(search_tokens("kiem soat con gian")) <= set(search_tokens(passage))
    assert tsquery("cơn giận") == "con | gian"


def test_chinese_is_searchable_without_spaces():
    assert "可道" in search_tokens("道可道，非常道") and "非常" in tsquery("非常道")


def test_stop_words_do_not_match_everything():
    assert search_tokens("the art of war and the laws") == ["art", "war", "laws"]


def test_citation_normalisation_ignores_quotes_case_and_punctuation():
    assert normalize_for_match("“Waste no more time arguing”—Marcus") == \
        normalize_for_match("waste no more time, arguing... marcus")


def test_an_explicit_first_section_does_not_duplicate_the_implicit_one():
    # Seneca's letter 1 carries '§1' although implicit_first already opened section 1.
    lv = levels_from_config([
        {"name": "letter", "pattern": r"^LETTER (\d+)$"},
        {"name": "sec", "pattern": r"^§(\d+) ", "inline": True, "implicit_first": True}])
    ps = split(["LETTER 1", "Greetings.", "§1 Continue to act thus.", "§2 Next."], lv)
    assert [(p.ref, p.text) for p in ps] == [("1.1", "Greetings. Continue to act thus."),
                                             ("1.2", "Next.")]
