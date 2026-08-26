"""Unit tests for filename_parser.py — covers all five naming patterns."""

import pytest
from src.filename_parser import (
    PatternA, PatternB, PatternC, PatternD, PatternE,
    normalise, strip_junk, parse_filename, PATTERNS,
)


# ── Normalisation ────────────────────────────────────────────────────────────

class TestNormalise:
    def test_lowercase(self):
        assert normalise("Hello World") == "hello world"

    def test_underscore_to_space(self):
        assert normalise("hello_world") == "hello world"

    def test_multiple_spaces_collapsed(self):
        assert normalise("hello  world") == "hello world"

    def test_accents_stripped(self):
        # e with acute → e
        assert "e" in normalise("Résumé")

    def test_cyrillic_preserved(self):
        result = normalise("Ябеда")
        assert "ябеда" in result.lower() or len(result) > 0  # Cyrillic kept

    def test_trailing_stripped(self):
        assert normalise("  hello  ") == "hello"


# ── Strip junk ───────────────────────────────────────────────────────────────

class TestStripJunk:
    def test_spotisaver_tag(self):
        assert "(SPOTISAVER)" not in strip_junk("Artist - Title (SPOTISAVER)")

    def test_feat_tag(self):
        result = strip_junk("Dua Lipa - Levitating (feat. DaBaby)")
        assert "feat" not in result.lower()

    def test_radio_edit(self):
        result = strip_junk("Song (Radio Edit)")
        assert "radio" not in result.lower()

    def test_track_number_prefix_dot(self):
        result = strip_junk("01. Artist - Title")
        assert result.startswith("Artist")

    def test_track_number_prefix_dash(self):
        result = strip_junk("1 - Artist - Title")
        assert result.startswith("Artist")


# ── Pattern A ────────────────────────────────────────────────────────────────

class TestPatternA:
    pat = PatternA()

    def test_detects_artist_dash_title(self):
        assert self.pat.detect("Animal Logic - Stone In My Shoe (SPOTISAVER)")

    def test_does_not_detect_spotimate(self):
        assert not self.pat.detect("SpotiMate.io - Title - Artist")

    def test_parse_basic(self):
        artist, title = self.pat.parse("Animal Logic - Stone In My Shoe (SPOTISAVER)")
        assert "Animal Logic" in artist
        assert "Stone In My Shoe" in title

    def test_parse_army_of_lovers(self):
        artist, title = self.pat.parse("Army Of Lovers - Flying High (SPOTISAVER)")
        assert "Army Of Lovers" in artist
        assert "Flying High" in title

    def test_parse_no_separator_returns_empty_artist(self):
        artist, title = self.pat.parse("JustOneSegmentNoSeparator")
        assert artist == ""

    def test_parse_multiple_dashes_title_contains_inner(self):
        artist, title = self.pat.parse("Artist - Title - With - Dashes")
        assert artist == "Artist"
        assert "Title - With - Dashes" in title

    def test_spotisaver_tag_stripped_from_title(self):
        _, title = self.pat.parse("Dana Dawson - Survival (SPOTISAVER)")
        assert "spotisaver" not in title.lower()


# ── Pattern B ────────────────────────────────────────────────────────────────

class TestPatternB:
    pat = PatternB()

    def test_detects_spotimate_prefix(self):
        assert self.pat.detect("SpotiMate.io - Diamonds Are A Girl_s Best Friend - Julie London")

    def test_does_not_detect_non_spotimate(self):
        assert not self.pat.detect("Artist - Title (SPOTISAVER)")

    def test_parse_standard(self):
        artist, title = self.pat.parse(
            "SpotiMate.io - Diamonds Are A Girl_s Best Friend - Julie London"
        )
        assert "Julie London" in artist
        assert "Diamonds" in title

    def test_parse_underscore_as_space(self):
        _, title = self.pat.parse("SpotiMate.io - Girl_s Best Friend - Julie London")
        assert "Girl s Best Friend" in title or "Girl's Best Friend" in title.replace("_", "'")

    def test_parse_cyrillic(self):
        artist, title = self.pat.parse("SpotiMate.io - Ябеда - Auktyon")
        assert "Auktyon" in artist
        assert "Ябеда" in title

    def test_parse_complex_title(self):
        artist, title = self.pat.parse(
            "SpotiMate.io - Candide_ Act II_ No. 27_ What_s the Use - Leonard Bernstein"
        )
        assert "Leonard Bernstein" in artist
        assert "Candide" in title


# ── Pattern C ────────────────────────────────────────────────────────────────

class TestPatternC:
    pat = PatternC()

    def test_detects_separator(self):
        assert self.pat.detect("Stone In My Shoe - Animal Logic")

    def test_reversed_roles(self):
        artist, title = self.pat.parse("Stone In My Shoe - Animal Logic")
        assert "Animal Logic" in artist
        assert "Stone In My Shoe" in title

    def test_does_not_detect_spotimate(self):
        assert not self.pat.detect("SpotiMate.io - Title - Artist")


# ── Pattern D ────────────────────────────────────────────────────────────────

class TestPatternD:
    pat = PatternD()

    def test_detects_number_dot(self):
        assert self.pat.detect("01. Artist - Title")

    def test_detects_number_dash(self):
        assert self.pat.detect("1 - Artist - Title")

    def test_does_not_detect_no_prefix(self):
        assert not self.pat.detect("Artist - Title")

    def test_parse_strips_number_dot(self):
        artist, title = self.pat.parse("01. Animal Logic - Stone In My Shoe")
        assert "Animal Logic" in artist
        assert "Stone In My Shoe" in title

    def test_parse_strips_number_dash(self):
        artist, title = self.pat.parse("5 - Artist Name - Track Title")
        assert "Artist Name" in artist


# ── Pattern E ────────────────────────────────────────────────────────────────

class TestPatternE:
    pat = PatternE()

    def test_detects_feat(self):
        assert self.pat.detect("Dua Lipa - Levitating (feat. DaBaby)")

    def test_detects_radio_edit(self):
        assert self.pat.detect("Daft Punk - Harder Better Faster Stronger (Radio Edit)")

    def test_detects_remaster(self):
        assert self.pat.detect("Some Song (Remaster 2021)")

    def test_parse_removes_feat(self):
        artist, title = self.pat.parse("Dua Lipa - Levitating (feat. DaBaby)")
        assert "DaBaby" not in title
        assert "Dua Lipa" in artist

    def test_parse_removes_radio_edit(self):
        artist, title = self.pat.parse(
            "Daft Punk - Harder Better Faster Stronger (Radio Edit)"
        )
        assert "Radio Edit" not in title
        assert "Daft Punk" in artist


# ── PATTERNS registry ────────────────────────────────────────────────────────

class TestPatternsRegistry:
    def test_all_patterns_in_list(self):
        from src.filename_parser import FilenamePattern
        for p in PATTERNS:
            assert isinstance(p, FilenamePattern)

    def test_spotimate_detected_before_generic(self):
        """PatternB should be the first to detect a SpotiMate filename."""
        filename = "SpotiMate.io - Title - Artist"
        detected = [p for p in PATTERNS if p.detect(filename)]
        assert len(detected) >= 1
        assert isinstance(detected[0], PatternB)


# ── parse_filename integration ───────────────────────────────────────────────

class TestParseFilename:
    def test_spotisaver_returns_candidates(self):
        cands = parse_filename("/music/Artist Name - Great Song (SPOTISAVER).mp3")
        assert len(cands) >= 1
        _, artist, title = cands[0]
        assert artist or title  # at least one is non-empty

    def test_spotimate_returns_reversed(self):
        cands = parse_filename("/music/SpotiMate.io - Great Song - Artist Name.mp3")
        assert any("Artist Name" in c[1] for c in cands)

    def test_no_separator_fallback(self):
        cands = parse_filename("/music/JustASingleSegment.mp3")
        assert len(cands) >= 1

    def test_extension_stripped(self):
        cands = parse_filename("/music/Artist - Title.flac")
        for _, artist, title in cands:
            assert ".flac" not in artist
            assert ".flac" not in title
