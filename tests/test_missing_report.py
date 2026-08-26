"""Tests for the missing/extra report: songs in the sheet but not uploaded,
and songs uploaded but not present on the sheet.
"""
from __future__ import annotations

import io
import tempfile
from pathlib import Path

from src.reporter import export_missing_report, _write_missing_sections


def _sample_match_table():
    return [
        {
            "sheet_row": 3, "track_title": "Song A", "artist_name": "Artist A",
            "album": "Album A", "spotify_link": "https://open.spotify.com/track/a",
            "spotify_dur_raw": "3:20", "matched_file": "/music/a.mp3",
        },
        {
            "sheet_row": 4, "track_title": "Song B", "artist_name": "Artist B",
            "album": "", "spotify_link": "https://open.spotify.com/track/b",
            "spotify_dur_raw": "2:10", "matched_file": None,
        },
        {
            "sheet_row": 5, "track_title": "Song C", "artist_name": "Artist C",
            "album": "", "spotify_link": "https://open.spotify.com/track/c",
            "spotify_dur_raw": "4:00", "matched_file": None,
        },
    ]


def _sample_unmatched_audio():
    return [
        {"path": "/music/extra_song.mp3", "duration": 187.4, "best_match_score": 12.0},
    ]


class TestMissingSections:
    def test_not_uploaded_lists_only_unmatched_sheet_rows(self):
        buf = io.StringIO()
        _write_missing_sections(buf, _sample_match_table(), _sample_unmatched_audio())
        text = buf.getvalue()
        assert "Song B" in text
        assert "Song C" in text
        assert "Song A" not in text.split("SONGS UPLOADED BUT NOT ON SHEET")[0]

    def test_not_on_sheet_lists_unmatched_audio(self):
        buf = io.StringIO()
        _write_missing_sections(buf, _sample_match_table(), _sample_unmatched_audio())
        text = buf.getvalue()
        assert "extra_song.mp3" in text
        assert "/music/extra_song.mp3" in text

    def test_empty_not_uploaded_shows_placeholder(self):
        all_matched = [dict(r, matched_file="/music/x.mp3") for r in _sample_match_table()]
        buf = io.StringIO()
        _write_missing_sections(buf, all_matched, [])
        text = buf.getvalue()
        assert "none — every sheet row has a matched file" in text
        assert "none — every uploaded file matched a sheet row" in text

    def test_export_missing_report_writes_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = export_missing_report(_sample_match_table(), _sample_unmatched_audio(), Path(tmp))
            assert path.exists()
            assert path.name.endswith("_MISSING_EXTRA.csv")
            content = path.read_text(encoding="utf-8")
            assert "SONGS IN SHEET BUT NOT UPLOADED" in content
            assert "SONGS UPLOADED BUT NOT ON SHEET" in content
            assert "Song B" in content
            assert "extra_song.mp3" in content
