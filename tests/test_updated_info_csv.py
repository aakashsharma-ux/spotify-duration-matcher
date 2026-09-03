"""Tests for reporter.py's 'Updated info csv' export."""

import csv
import io
from pathlib import Path

import pytest

from src.reporter import (
    read_raw_csv_template,
    build_updated_info_rows,
    export_updated_info_csv,
    UPDATED_INFO_TARGETS,
)

# The exact real-world header the tool needs to round-trip, columns E-H/J-N
# etc. included, so this is a faithful shape test rather than a toy example.
REAL_HEADER = [
    "track_title", "artist_name", "album", "release_date", "popularity",
    "genres", "styles", "country", "spotify_link", "DA NAME", "DATE",
    "STATUS", "Site used", "# of listens", "track duration spotify",
    "track duration file", "difference", ">1s difference?",
    "uploaded successfully?", "issues", "notes", "ID TO USE",
    "tool da - 17 feb", "ORIGINAL DA", "ORIGINAL DATE", "",
    "SPOTIFY TITLE", "SPOTIFY ARTIST",
]


def write_csv(path: Path, banner, header, rows):
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(banner)
        w.writerow(header)
        w.writerows(rows)


@pytest.fixture
def sample_csv(tmp_path):
    """A 2-row CSV using the real header, with pre-existing (now-stale)
    values in the columns this tool is supposed to correct, plus a manually
    filled 'uploaded successfully?' that must survive untouched, plus a
    trailing blank row that sheet_loader itself would skip."""
    path = tmp_path / "sheet.csv"
    rows = [
        # track_title, artist_name, album, release_date, popularity, genres,
        # styles, country, spotify_link, DA NAME, DATE, STATUS, Site used,
        # # of listens, track duration spotify, track duration file,
        # difference, >1s difference?, uploaded successfully?, issues,
        # notes, ID TO USE, tool da - 17 feb, ORIGINAL DA, ORIGINAL DATE,
        # (blank), SPOTIFY TITLE, SPOTIFY ARTIST
        ["Lover", "The Troggs", "Some Album", "2020-01-01", "42", "rock",
         "garage", "UK", "https://open.spotify.com/track/x", "Jane", "2026-01-01",
         "done", "SPOTISAVER", "5", "3:42", "0:00", "STALE", "", "YES",
         "", "some note", "ID1", "tool-a", "OrigDA", "2025-12-01", "",
         "Lover", "The Troggs"],
        ["Yesterday", "The Beatles", "Help", "1965-01-01", "88", "pop",
         "rock", "UK", "https://open.spotify.com/track/y", "Jane", "2026-01-01",
         "pending", "SPOTISAVER", "9", "2:05", "", "", "", "NO",
         "needs review", "", "ID2", "tool-b", "OrigDA2", "2025-12-02", "",
         "Yesterday", "The Beatles"],
        ["", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "",
         "", "", "", "", "", "", "", "", "", "", "", ""],  # blank row
    ]
    write_csv(path, ["Banner"], REAL_HEADER, rows)
    return path


def make_match_table():
    return [
        {"sheet_row": 3, "track_title": "Lover", "artist_name": "The Troggs",
         "file_dur": 222.4, "diff": 0.4, "flag_gt1s": False, "matched_file": "/m/a.mp3"},
        {"sheet_row": 4, "track_title": "Yesterday", "artist_name": "The Beatles",
         "file_dur": 130.0, "diff": 5.0, "flag_gt1s": True, "matched_file": "/m/b.mp3"},
    ]


class TestReadRawCsvTemplate:
    def test_captures_banner_header_and_rows(self, sample_csv):
        banner, header, raw_rows = read_raw_csv_template(str(sample_csv))
        assert banner == ["Banner"]
        assert header == REAL_HEADER
        assert set(raw_rows.keys()) == {3, 4, 5}

    def test_missing_file_raises(self):
        with pytest.raises(FileNotFoundError):
            read_raw_csv_template("/no/such/file.csv")

    def test_blank_row_captured_verbatim(self, sample_csv):
        _, _, raw_rows = read_raw_csv_template(str(sample_csv))
        assert all(c == "" for c in raw_rows[5])


class TestBuildUpdatedInfoRows:
    def test_patches_only_the_three_target_columns(self, sample_csv):
        banner, header, raw_rows = read_raw_csv_template(str(sample_csv))
        out = build_updated_info_rows(header, raw_rows, make_match_table())

        idx = {name: header.index(name) for name in REAL_HEADER if name}
        row1 = out[0]  # physical row 3, Lover
        assert row1[idx["track duration file"]] == "3:42"   # 222.4s -> 3:42
        assert row1[idx["difference"]] == "0.40"
        assert row1[idx[">1s difference?"]] == ""            # flag_gt1s False

        row2 = out[1]  # physical row 4, Yesterday
        assert row2[idx["track duration file"]] == "2:10"    # 130.0s -> 2:10
        assert row2[idx["difference"]] == "5.00"
        assert row2[idx[">1s difference?"]] == "YES"

    def test_never_touches_uploaded_successfully_column(self, sample_csv):
        """This column is explicitly excluded — must survive byte-for-byte."""
        banner, header, raw_rows = read_raw_csv_template(str(sample_csv))
        out = build_updated_info_rows(header, raw_rows, make_match_table())
        idx = header.index("uploaded successfully?")
        assert out[0][idx] == "YES"   # untouched, even though row 1 was matched
        assert out[1][idx] == "NO"    # untouched, even though row 2 was matched

    def test_passes_through_every_other_column_unchanged(self, sample_csv):
        banner, header, raw_rows = read_raw_csv_template(str(sample_csv))
        out = build_updated_info_rows(header, raw_rows, make_match_table())
        untouched_names = [
            "track_title", "artist_name", "album", "release_date", "popularity",
            "genres", "styles", "country", "spotify_link", "DA NAME", "DATE",
            "STATUS", "Site used", "# of listens", "track duration spotify",
            "issues", "notes", "ID TO USE", "tool da - 17 feb", "ORIGINAL DA",
            "ORIGINAL DATE", "SPOTIFY TITLE", "SPOTIFY ARTIST",
        ]
        for name in untouched_names:
            i = header.index(name)
            assert out[0][i] == raw_rows[3][i], f"column {name!r} changed unexpectedly"
            assert out[1][i] == raw_rows[4][i], f"column {name!r} changed unexpectedly"

    def test_unmatched_row_passed_through_verbatim(self, sample_csv):
        """A row with no match_table entry (blank row) stays exactly as read."""
        banner, header, raw_rows = read_raw_csv_template(str(sample_csv))
        out = build_updated_info_rows(header, raw_rows, make_match_table())
        assert out[2] == raw_rows[5]

    def test_row_with_no_file_match_leaves_target_columns_blank(self, sample_csv):
        """A sheet row present in match_table but never matched to a file —
        file_dur/diff are None — should blank the target cells, not crash
        or leave stale pre-existing values."""
        banner, header, raw_rows = read_raw_csv_template(str(sample_csv))
        match_table = [
            {"sheet_row": 3, "track_title": "Lover", "artist_name": "The Troggs",
             "file_dur": None, "diff": None, "flag_gt1s": False, "matched_file": None},
        ]
        out = build_updated_info_rows(header, raw_rows, match_table)
        idx = {name: header.index(name) for name in ["track duration file", "difference"]}
        assert out[0][idx["track duration file"]] == ""
        assert out[0][idx["difference"]] == ""

    def test_row_count_and_order_preserved(self, sample_csv):
        banner, header, raw_rows = read_raw_csv_template(str(sample_csv))
        out = build_updated_info_rows(header, raw_rows, make_match_table())
        assert len(out) == 3  # 2 data rows + 1 blank row, same as source

    def test_missing_target_column_is_a_noop_not_a_crash(self):
        """A header that simply doesn't have one of the target columns —
        e.g. a stripped-down CSV — must not error; that column is just
        never touched."""
        header = ["track_title", "artist_name"]
        raw_rows = {3: ["Lover", "The Troggs"]}
        match_table = [{"sheet_row": 3, "file_dur": 222.0, "diff": 0.0, "flag_gt1s": False}]
        out = build_updated_info_rows(header, raw_rows, match_table)
        assert out == [["Lover", "The Troggs"]]

    def test_short_row_is_padded_not_indexerror(self):
        """A row shorter than the header (ragged CSV) must be padded, not crash."""
        header = ["track_title", "artist_name", "track duration file"]
        raw_rows = {3: ["Lover", "The Troggs"]}  # missing the 3rd cell entirely
        match_table = [{"sheet_row": 3, "file_dur": 222.0, "diff": 0.0, "flag_gt1s": False}]
        out = build_updated_info_rows(header, raw_rows, match_table)
        assert out == [["Lover", "The Troggs", "3:42"]]


class TestExportUpdatedInfoCsv:
    def test_writes_full_file_with_banner_header_and_rows(self, sample_csv, tmp_path):
        out_dir = tmp_path / "out"
        path = export_updated_info_csv(str(sample_csv), make_match_table(), out_dir)
        assert path.exists()
        with path.open(newline="", encoding="utf-8") as fh:
            rows = list(csv.reader(fh))
        assert rows[0] == ["Banner"]
        assert rows[1] == REAL_HEADER
        assert len(rows) == 5  # banner + header + 3 data rows

    def test_output_is_reloadable_as_a_valid_csv(self, sample_csv, tmp_path):
        """The whole point: this file should be usable as a fresh upload."""
        path = export_updated_info_csv(str(sample_csv), make_match_table(), tmp_path / "out")
        reloaded_banner, reloaded_header, reloaded_rows = read_raw_csv_template(str(path))
        assert reloaded_header == REAL_HEADER
        assert len(reloaded_rows) == 3


def test_updated_info_targets_excludes_uploaded_successfully():
    """Guard against accidentally re-adding the protected column later."""
    assert "uploaded successfully?" not in UPDATED_INFO_TARGETS
    assert "track duration file" in UPDATED_INFO_TARGETS
    assert "difference" in UPDATED_INFO_TARGETS
    assert ">1s difference?" in UPDATED_INFO_TARGETS
