"""Unit tests for matcher.py — fuzzy matching engine."""

import pytest
from unittest.mock import patch
from src.matcher import match_tracks, _combined_score, _compute_diff_and_flag, UNMATCHED_LABEL


def make_track(row=1, title="Test Track", artist="Test Artist", spotify_dur=222.0, spotify_dur_raw="3:42"):
    return {"sheet_row": row, "track_title": title, "artist_name": artist,
            "album": "Album", "spotify_link": "", "spotify_dur": spotify_dur, "spotify_dur_raw": spotify_dur_raw}


def make_audio(path="/music/Test Artist - Test Track.mp3", duration=222.5, status="OK"):
    return {"path": path, "filename": path.split("/")[-1], "extension": ".mp3",
            "duration": duration, "status": status, "used": False}


# ── _combined_score ──────────────────────────────────────────────────────────

class TestCombinedScore:
    def test_perfect_match(self):
        assert _combined_score("Test Track", "Test Artist", "Test Track", "Test Artist") == pytest.approx(100.0, abs=1.0)

    def test_zero_match(self):
        assert _combined_score("zzzzz", "qqqqq", "Test Track", "Test Artist") < 30

    def test_title_outweighs_artist(self):
        title_only  = _combined_score("Test Track", "nobody",      "Test Track", "Test Artist")
        artist_only = _combined_score("nothing",    "Test Artist", "Test Track", "Test Artist")
        assert title_only > artist_only

    def test_case_insensitive(self):
        s1 = _combined_score("test track", "test artist", "Test Track", "Test Artist")
        s2 = _combined_score("TEST TRACK", "TEST ARTIST", "Test Track", "Test Artist")
        assert abs(s1 - s2) < 2.0


# ── _compute_diff_and_flag ───────────────────────────────────────────────────

class TestComputeDiffAndFlag:
    @pytest.mark.parametrize("file_dur,spotify_dur,tol,exp_flag", [
        (222.0, 221.0, 1.0, False),   # exactly 1 s — NOT flagged
        (223.1, 222.0, 1.0, True),    # 1.1 s over → flagged
        (220.5, 222.0, 1.0, True),    # -1.5 s → flagged
        (222.0, 222.0, 1.0, False),
        (None,  222.0, 1.0, False),
        (222.0, None,  1.0, False),
    ])
    def test_microseconds_on(self, file_dur, spotify_dur, tol, exp_flag):
        _, flag = _compute_diff_and_flag(file_dur, spotify_dur, tol, use_microseconds=True)
        assert flag == exp_flag

    def test_microseconds_off_same_floor(self):
        """5:03.8 vs 5:03.0 — floor both → 303-303=0 → NOT flagged."""
        diff, flag = _compute_diff_and_flag(303.8, 303.0, 1.0, use_microseconds=False)
        assert flag is False
        assert diff == pytest.approx(0.8, abs=0.01)   # precise diff still stored

    def test_microseconds_off_genuinely_different(self):
        """5:05 vs 5:03 — floor → 305-303=2 → flagged."""
        _, flag = _compute_diff_and_flag(305.0, 303.0, 1.0, use_microseconds=False)
        assert flag is True

    def test_precise_diff_always_stored(self):
        diff, _ = _compute_diff_and_flag(303.8, 303.0, 1.0, use_microseconds=False)
        assert diff == pytest.approx(0.8, abs=0.01)


# ── match_tracks (returns tuple) ─────────────────────────────────────────────

class TestMatchTracks:
    def test_returns_tuple(self):
        result = match_tracks([make_track()], [make_audio()])
        assert isinstance(result, tuple) and len(result) == 2

    def test_basic_match(self):
        table, unmatched = match_tracks([make_track()], [make_audio()])
        assert table[0]["status"] != UNMATCHED_LABEL
        assert len(unmatched) == 0

    def test_unmatched_when_no_audio(self):
        table, unmatched = match_tracks([make_track()], [])
        assert table[0]["status"] == UNMATCHED_LABEL
        assert len(unmatched) == 0

    def test_extra_csv_rows_become_unmatched(self):
        """CSV with 3 tracks but only 1 audio file → 2 UNMATCHED, 1 unused audio."""
        tracks = [make_track(row=i, title=f"Track {i}", artist=f"Artist {i}") for i in range(3)]
        audio  = [make_audio(path="/music/Test Artist - Test Track.mp3")]
        table, unmatched = match_tracks(tracks, audio, threshold=55)
        matched_count  = sum(1 for r in table if r["status"] != UNMATCHED_LABEL)
        unmatched_rows = sum(1 for r in table if r["status"] == UNMATCHED_LABEL)
        assert matched_count  >= 1
        assert unmatched_rows >= 1   # extra CSV rows are UNMATCHED

    def test_unused_audio_returned(self):
        """2 audio files, 1 track → 1 unused audio file returned."""
        tracks = [make_track()]
        audio  = [make_audio(path="/music/Test Artist - Test Track.mp3"),
                  make_audio(path="/music/Other - Song.mp3", duration=100.0)]
        _, unmatched = match_tracks(tracks, audio)
        assert len(unmatched) >= 1

    def test_one_to_one_no_double_assign(self):
        tracks = [make_track(row=1, title="Song", artist="A"),
                  make_track(row=2, title="Song", artist="A")]
        audio  = [make_audio(path="/music/A - Song.mp3")]
        table, _ = match_tracks(tracks, audio)
        matched = [r for r in table if r["matched_file"] is not None]
        assert len(matched) <= 1

    def test_microseconds_flag_propagates(self):
        tracks = [make_track(spotify_dur=303.0)]
        audio  = [make_audio(duration=303.8)]
        table, _ = match_tracks(tracks, audio, tolerance=1.0, use_microseconds=False)
        if table[0]["matched_file"]:
            assert table[0]["flag_gt1s"] is False   # 0.8s should NOT flag

    def test_preserves_sheet_order(self):
        tracks = [make_track(row=3, title="C", artist="C"),
                  make_track(row=1, title="A", artist="A"),
                  make_track(row=2, title="B", artist="B")]
        audio  = [make_audio(f"/m/A - A.mp3"), make_audio(f"/m/B - B.mp3"), make_audio(f"/m/C - C.mp3")]
        table, _ = match_tracks(tracks, audio)
        assert [r["sheet_row"] for r in table] == [3, 1, 2]

    def test_spotimate_cyrillic_matched(self):
        tracks = [make_track(title="Ябеда", artist="Auktyon")]
        audio  = [make_audio(path="/music/SpotiMate.io - Ябеда - Auktyon.mp3")]
        table, _ = match_tracks(tracks, audio, threshold=55)
        assert table[0]["status"] != UNMATCHED_LABEL

    def test_threshold_boundary_accepted(self):
        tracks = [make_track()]; audio = [make_audio()]
        with patch("src.matcher._combined_score", return_value=55.0):
            table, _ = match_tracks(tracks, audio, threshold=55)
        assert table[0]["status"] != UNMATCHED_LABEL

    def test_threshold_boundary_rejected(self):
        tracks = [make_track()]; audio = [make_audio()]
        with patch("src.matcher._combined_score", return_value=54.9):
            table, _ = match_tracks(tracks, audio, threshold=55)
        assert table[0]["status"] == UNMATCHED_LABEL

    def test_track_number_boost(self):
        # Even with low direct combined score, if it matches the track number, it gets boosted and matched.
        tracks = [make_track(row=3, title="Stone Shoe", artist="Animal Logic")]
        # Title/artist have some basic similarity but not enough on their own
        audio = [make_audio(path="/music/003 - Animal - Stone.mp3")]
        table, unmatched = match_tracks(tracks, audio, threshold=55)
        assert table[0]["status"] != UNMATCHED_LABEL
        assert table[0]["matched_file"] == "/music/003 - Animal - Stone.mp3"
        assert table[0]["match_score"] == 100.0

    def test_compute_unmatched_status_categorization(self):
        tracks = [make_track(row=3, title="Stone Shoe", artist="Animal Logic")]
        audio = [
            make_audio(path="/music/Some completely different song.mp3"),  # NOT_IN_CSV
            make_audio(path="/music/Stone - Animal.mp3")  # NO_MATCH (similarity but score < 55)
        ]
        # Set a high threshold so they both remain unmatched
        table, unmatched = match_tracks(tracks, audio, threshold=95)
        assert len(unmatched) == 2
        # Verify status categorization
        unmatched_by_path = {f["path"]: f for f in unmatched}
        assert unmatched_by_path["/music/Some completely different song.mp3"]["match_status"] == "NOT_IN_CSV"
        assert unmatched_by_path["/music/Stone - Animal.mp3"]["match_status"] == "NO_MATCH"
