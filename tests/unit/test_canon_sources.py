"""C-01: only sources whose year, URL and license were verified are ever loaded."""
import pytest

from canon.ingest import CanonError, Source, load_sources, load_work


def src(**over):
    base = dict(id="w.test", work="W", work_vi="W", author="A", translator=None, pub_year=1862,
                language="en", pillar="stoic", license="public_domain", status="unverified",
                verified_on=None, source_url="u", fetch=(), format="text", parser={},
                ref_label="{item}", align=None, evidence=())
    return Source(**{**base, **over})


class RecordingCursor:
    def __init__(self):
        self.sql = []
        self.rowcount = 0

    def execute(self, sql, params=None):
        self.sql.append(sql)


def test_an_unverified_source_is_never_loaded():
    from canon.chunk import Passage
    cur = RecordingCursor()
    with pytest.raises(CanonError, match="only verified"):
        load_work(cur, src(), [Passage({"item": 1}, "text")])
    assert cur.sql == []


def test_a_verified_source_needs_its_evidence(tmp_path):
    p = tmp_path / "s.yaml"
    p.write_text("""sources:
  - {id: x.y, work: W, work_vi: W, author: A, pub_year: 1862, language: en, pillar: stoic,
     license: public_domain, status: verified, source_url: u, ref_label: "{item}"}
""", encoding="utf-8")
    from youtube_intel.config import ConfigError
    with pytest.raises(ConfigError, match="evidence"):
        load_sources(p)


def test_shipped_source_list_is_valid():
    sources = load_sources()
    assert sources and all(s.license in ("public_domain", "own") for s in sources)
    for s in sources:
        if s.status == "verified":
            assert s.verified_on and s.evidence and s.fetch and s.parser.get("levels")




def test_excluded_sources_say_why():
    for s in load_sources():
        if s.status == "excluded":
            assert s.evidence, s.id


def test_the_basore_translation_is_never_loaded():
    # Spec §9: Basore's later volumes may still be under US copyright (C-01).
    assert all(s.translator != "John W. Basore" or s.status == "excluded" for s in load_sources())
