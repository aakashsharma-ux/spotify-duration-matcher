"""Unit tests for renamer.py — covers the stats-tuple return and all edge cases
that previously caused the Rename feature to silently do nothing.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from src.renamer import rename_matched_files, _build_new_name, _slugify


# ── _slugify ─────────────────────────────────────────────────────────────────

class TestSlugify:
    def test_preserves_spaces(self):
        assert _slugify("Stone In My Shoe") == "Stone In My Shoe"

    def test_strips_unsafe_chars(self):
        assert _slugify('Bad:Name/Here?') == "BadNameHere"

    def test_collapses_multiple_spaces(self):
        assert _slugify("Too   Many   Spaces") == "Too Many Spaces"

    def test_truncates_long_names(self):
        long_name = "A" * 100
        assert len(_slugify(long_name)) == 60


# ── _build_new_name ──────────────────────────────────────────────────────────

class TestBuildNewName:
    def test_title_artist_format(self):
        name = _build_new_name(3, "Animal Logic", "Stone In My Shoe", ".mp3",
                               "{idx:03d} - {title} - {artist}{ext}")
        assert name == "003 - Stone In My Shoe - Animal Logic.mp3"

    def test_artist_title_format(self):
        name = _build_new_name(3, "Animal Logic", "Stone In My Shoe", ".mp3",
                               "{idx:03d} - {artist} - {title}{ext}")
        assert name == "003 - Animal Logic - Stone In My Shoe.mp3"

    def test_no_serial_format(self):
        name = _build_new_name(3, "Animal Logic", "Stone In My Shoe", ".mp3",
                               "{title} - {artist}{ext}")
        assert name == "Stone In My Shoe - Animal Logic.mp3"

    def test_bad_template_falls_back(self):
        name = _build_new_name(1, "A", "B", ".mp3", "{bogus_key}{ext}")
        assert name == "001 - B - A.mp3"


# ── rename_matched_files — return signature ──────────────────────────────────

class TestRenameReturnsTuple:
    def test_returns_match_table_and_stats(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "Artist - Title.mp3"
            src.write_bytes(b"\xff\xfb" * 10)
            table = [{"sheet_row": 1, "track_title": "Title", "artist_name": "Artist",
                      "matched_file": str(src), "suggested_rename": ""}]
            result = rename_matched_files(table, "{idx:03d} - {title} - {artist}{ext}")
            assert isinstance(result, tuple) and len(result) == 2
            returned_table, stats = result
            assert isinstance(stats, dict)
            assert "renamed" in stats

    def test_stats_has_all_expected_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            table = []
            _, stats = rename_matched_files(table, "{idx:03d}{ext}")
            for key in ("renamed", "skipped_same_name", "skipped_not_found",
                       "skipped_dest_exists", "errors"):
                assert key in stats


# ── Core renaming behaviour ────────────────────────────────────────────────────

class TestRenameBehavior:
    def test_basic_rename_succeeds(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "Animal Logic - Stone In My Shoe (SPOTISAVER).mp3"
            src.write_bytes(b"\xff\xfb" * 10)
            table = [{"sheet_row": 3, "track_title": "Stone In My Shoe",
                      "artist_name": "Animal Logic", "matched_file": str(src),
                      "suggested_rename": ""}]
            table, stats = rename_matched_files(table, "{idx:03d} - {title} - {artist}{ext}")

            expected = Path(tmp) / "003 - Stone In My Shoe - Animal Logic.mp3"
            assert expected.exists(), "Renamed file should exist on disk"
            assert stats["renamed"] == 1
            assert table[0]["matched_file"] == str(expected)

    def test_rename_with_absolute_path(self):
        """Regression: ensure rename works when matched_file is a fully
        resolved absolute path (the normal case from audio_scanner)."""
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp).resolve() / "Artist - Title.mp3"
            src.write_bytes(b"\xff\xfb" * 10)
            table = [{"sheet_row": 1, "track_title": "Title", "artist_name": "Artist",
                      "matched_file": str(src), "suggested_rename": ""}]
            table, stats = rename_matched_files(table, "{idx:03d} - {title} - {artist}{ext}")
            assert stats["renamed"] == 1

    def test_unmatched_row_skipped_silently_no_error(self):
        """Rows with no matched_file should not appear as errors."""
        table = [{"sheet_row": 5, "track_title": "Missing", "artist_name": "Nobody",
                  "matched_file": None, "suggested_rename": ""}]
        table, stats = rename_matched_files(table, "{idx:03d} - {title} - {artist}{ext}")
        assert stats["renamed"] == 0
        assert stats["errors"] == []
        # suggested_rename should still be populated for display purposes
        assert table[0]["suggested_rename"] == "005 - Missing - Nobody"

    def test_already_correct_name_reports_skipped_same_name(self):
        """If a file is already named correctly, it should be reported as
        skipped_same_name, NOT as an error, and NOT silently invisible."""
        with tempfile.TemporaryDirectory() as tmp:
            already_correct = Path(tmp) / "003 - Stone In My Shoe - Animal Logic.mp3"
            already_correct.write_bytes(b"\xff\xfb" * 10)
            table = [{"sheet_row": 3, "track_title": "Stone In My Shoe",
                      "artist_name": "Animal Logic", "matched_file": str(already_correct),
                      "suggested_rename": ""}]
            table, stats = rename_matched_files(table, "{idx:03d} - {title} - {artist}{ext}")
            assert stats["renamed"] == 0
            assert stats["skipped_same_name"] == 1
            assert already_correct.exists()  # file untouched, still there

    def test_missing_source_file_reports_not_found(self):
        table = [{"sheet_row": 1, "track_title": "Ghost", "artist_name": "Nowhere",
                  "matched_file": "/nonexistent/path/Ghost - Nowhere.mp3",
                  "suggested_rename": ""}]
        table, stats = rename_matched_files(table, "{idx:03d} - {title} - {artist}{ext}")
        assert stats["skipped_not_found"] == 1
        assert stats["renamed"] == 0
        assert len(stats["errors"]) == 1

    def test_destination_collision_reports_dest_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "Artist - Title.mp3"
            src.write_bytes(b"\xff\xfb" * 10)
            # Pre-create the destination file to force a collision
            dest = Path(tmp) / "001 - Title - Artist.mp3"
            dest.write_bytes(b"\xff\xfb" * 10)

            table = [{"sheet_row": 1, "track_title": "Title", "artist_name": "Artist",
                      "matched_file": str(src), "suggested_rename": ""}]
            table, stats = rename_matched_files(table, "{idx:03d} - {title} - {artist}{ext}")
            assert stats["skipped_dest_exists"] == 1
            assert stats["renamed"] == 0
            assert src.exists(), "Source must remain untouched on collision"

    def test_multiple_files_mixed_outcomes(self):
        """Realistic batch: one renames fine, one already correct, one missing."""
        with tempfile.TemporaryDirectory() as tmp:
            f1 = Path(tmp) / "Animal Logic - Stone In My Shoe.mp3"
            f1.write_bytes(b"x")
            f2 = Path(tmp) / "002 - Flying High - Army Of Lovers.mp3"
            f2.write_bytes(b"x")

            table = [
                {"sheet_row": 1, "track_title": "Stone In My Shoe", "artist_name": "Animal Logic",
                 "matched_file": str(f1), "suggested_rename": ""},
                {"sheet_row": 2, "track_title": "Flying High", "artist_name": "Army Of Lovers",
                 "matched_file": str(f2), "suggested_rename": ""},
                {"sheet_row": 3, "track_title": "Ghost Track", "artist_name": "Nobody",
                 "matched_file": "/no/such/file.mp3", "suggested_rename": ""},
            ]
            table, stats = rename_matched_files(table, "{idx:03d} - {title} - {artist}{ext}")
            assert stats["renamed"] == 1
            assert stats["skipped_same_name"] == 1
            assert stats["skipped_not_found"] == 1

    def test_unicode_filenames_rename_correctly(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "SpotiMate.io - Ябеда - Auktyon.mp3"
            src.write_bytes(b"x")
            table = [{"sheet_row": 1, "track_title": "Ябеда", "artist_name": "Auktyon",
                      "matched_file": str(src), "suggested_rename": ""}]
            table, stats = rename_matched_files(table, "{idx:03d} - {title} - {artist}{ext}")
            assert stats["renamed"] == 1
            assert "Ябеда" in table[0]["matched_file"]

    def test_dry_run_does_not_touch_disk(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "Artist - Title.mp3"
            src.write_bytes(b"x")
            table = [{"sheet_row": 1, "track_title": "Title", "artist_name": "Artist",
                      "matched_file": str(src), "suggested_rename": ""}]
            table, stats = rename_matched_files(table, "{idx:03d} - {title} - {artist}{ext}",
                                                dry_run=True)
            assert src.exists(), "Source file must remain in dry-run mode"
            assert stats["renamed"] == 1   # counted but not executed

    def test_all_seven_format_presets(self):
        """Every format listed in the web UI dropdown must produce a valid name."""
        formats = {
            "{idx:03d} - {title} - {artist}{ext}": "001 - Title Here - Artist Here.mp3",
            "{idx:03d} - {artist} - {title}{ext}": "001 - Artist Here - Title Here.mp3",
            "{title} - {artist}{ext}":             "Title Here - Artist Here.mp3",
            "{artist} - {title}{ext}":             "Artist Here - Title Here.mp3",
            "{idx:03d} - {title}{ext}":            "001 - Title Here.mp3",
            "{idx:03d} - {artist}{ext}":           "001 - Artist Here.mp3",
            "{title}{ext}":                        "Title Here.mp3",
        }
        for tpl, expected in formats.items():
            got = _build_new_name(1, "Artist Here", "Title Here", ".mp3", tpl)
            assert got == expected, f"Template {tpl!r} produced {got!r}, expected {expected!r}"
