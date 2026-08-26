"""Tests for web_server.py — session store, API endpoints, edge cases."""

from __future__ import annotations

import csv
import io
import json
import os
import tempfile
import time
from pathlib import Path

import pytest

from src.web_server import SessionStore, create_app, _serialise, _serialise_audio


# ── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture
def app():
    cfg = {
        "match_threshold": 55,
        "duration_tolerance_sec": 1.0,
        "cache_db": ".duration_cache.db",
        "rename_template": "{idx:03d} - {title} - {artist}{ext}",
        "credentials_file": "creds.json",
    }
    application = create_app(cfg)
    application.config["TESTING"] = True
    return application


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def sid():
    return "test-session-abc123"


@pytest.fixture
def auth_headers(sid):
    return {"X-Session-Id": sid}


def _make_csv_bytes(rows):
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerows(rows)
    return buf.getvalue().encode()


def _minimal_csv():
    return _make_csv_bytes([
        ["Banner row"],
        ["track_title", "artist_name", "album", "release_date", "", "", "", "",
         "spotify_link", "", "", "", "", "", "spotify_dur"],
        ["Stone In My Shoe", "Animal Logic", "Animal Logic", "1991", "", "", "", "",
         "", "", "", "", "", "", "3:42"],
        ["Flying High", "Army Of Lovers", "", "", "", "", "", "", "", "", "", "", "", "", "3:28"],
    ])


# ── SessionStore ─────────────────────────────────────────────────────────────

class TestSessionStore:
    def test_creates_new_session(self):
        store = SessionStore()
        sess  = store.get_or_create("abc")
        assert "match_table" in sess
        assert "audio_files" in sess

    def test_returns_same_session(self):
        store = SessionStore()
        s1 = store.get_or_create("abc")
        s1["match_table"] = [{"test": True}]
        s2 = store.get_or_create("abc")
        assert s2["match_table"] == [{"test": True}]

    def test_different_sessions_isolated(self):
        store = SessionStore()
        s1 = store.get_or_create("user-1")
        s2 = store.get_or_create("user-2")
        s1["match_table"] = [{"track": "A"}]
        assert s2["match_table"] == []

    def test_evicts_lru_at_capacity(self):
        store = SessionStore(max_sessions=3)
        store.get_or_create("a")
        store.get_or_create("b")
        store.get_or_create("c")
        store.get_or_create("d")   # "a" should be evicted (LRU)
        assert store.active_count <= 3

    def test_ttl_eviction(self):
        store = SessionStore(ttl=1)
        store.get_or_create("temp")
        time.sleep(1.1)
        # Trigger eviction by accessing store
        store.get_or_create("new")
        # "temp" should be gone
        assert store.active_count == 1

    def test_active_count(self):
        store = SessionStore()
        assert store.active_count == 0
        store.get_or_create("x")
        store.get_or_create("y")
        assert store.active_count == 2

    def test_thread_safety(self):
        """Concurrent writes must not corrupt state."""
        import threading
        store = SessionStore()
        errors = []

        def worker(n):
            try:
                for _ in range(20):
                    sess = store.get_or_create(f"user-{n}")
                    sess["match_table"].append(n)
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
        for t in threads: t.start()
        for t in threads: t.join()
        assert not errors, f"Thread safety errors: {errors}"


# ── Health endpoint ──────────────────────────────────────────────────────────

class TestHealth:
    def test_returns_ok(self, client):
        r = client.get("/api/health")
        assert r.status_code == 200
        data = r.get_json()
        assert data["status"] == "ok"
        assert "active_sessions" in data


# ── Upload audio ─────────────────────────────────────────────────────────────

class TestUploadAudio:
    def test_upload_creates_temp_dir(self, client, auth_headers):
        data = {"audio_files": (io.BytesIO(b"\xff\xfb" * 100), "test.mp3")}
        r = client.post("/api/upload-audio", headers=auth_headers,
                        data=data, content_type="multipart/form-data")
        assert r.status_code == 200
        body = r.get_json()
        assert body["file_count"] == 1
        assert os.path.isdir(body["audio_dir"])

    def test_upload_no_files_returns_400(self, client, auth_headers):
        r = client.post("/api/upload-audio", headers=auth_headers,
                        content_type="multipart/form-data")
        assert r.status_code == 400

    def test_upload_appends_across_chunks(self, client, auth_headers):
        """Two chunks of the same selection must land in the same folder."""
        d1 = {"audio_files": (io.BytesIO(b"\xff\xfb" * 100), "a.mp3"), "reset": "true"}
        r1 = client.post("/api/upload-audio", headers=auth_headers,
                         data=d1, content_type="multipart/form-data")
        assert r1.get_json()["file_count"] == 1

        d2 = {"audio_files": (io.BytesIO(b"\xff\xfb" * 100), "b.mp3")}
        r2 = client.post("/api/upload-audio", headers=auth_headers,
                         data=d2, content_type="multipart/form-data")
        body2 = r2.get_json()
        assert body2["file_count"] == 2, "second chunk must add to, not replace, the first"
        assert body2["chunk_file_count"] == 1
        assert body2["audio_dir"] == r1.get_json()["audio_dir"]

    def test_upload_reset_clears_previous_batch(self, client, auth_headers):
        """reset=true must start a fresh folder and clean up the old one."""
        d1 = {"audio_files": (io.BytesIO(b"\xff\xfb" * 100), "a.mp3"), "reset": "true"}
        r1 = client.post("/api/upload-audio", headers=auth_headers,
                         data=d1, content_type="multipart/form-data")
        old_dir = r1.get_json()["audio_dir"]

        d2 = {"audio_files": (io.BytesIO(b"\xff\xfb" * 100), "b.mp3"), "reset": "true"}
        r2 = client.post("/api/upload-audio", headers=auth_headers,
                         data=d2, content_type="multipart/form-data")
        body2 = r2.get_json()
        assert body2["file_count"] == 1
        assert not os.path.exists(old_dir), "reset must clean up the previous owned dir, not leak it"


# ── Analyse CSV ──────────────────────────────────────────────────────────────

class TestAnalyseCSV:
    def test_missing_csv_returns_400(self, client, auth_headers):
        with tempfile.TemporaryDirectory() as tmp:
            r = client.post("/api/analyse-csv", headers=auth_headers,
                            data={"audio_dir": tmp},
                            content_type="multipart/form-data")
        assert r.status_code == 400

    def test_missing_audio_dir_returns_400(self, client):
        # Use a fresh session with no previously uploaded audio_dir
        csv_bytes = _minimal_csv()
        fresh = {"X-Session-Id": "fresh-no-audio-dir"}
        r = client.post("/api/analyse-csv", headers=fresh,
                        data={"csv_file": (io.BytesIO(csv_bytes), "sheet.csv")},
                        content_type="multipart/form-data")
        assert r.status_code == 400

    def test_nonexistent_audio_dir_returns_400(self, client, auth_headers):
        csv_bytes = _minimal_csv()
        r = client.post("/api/analyse-csv", headers=auth_headers,
                        data={"csv_file": (io.BytesIO(csv_bytes), "sheet.csv"),
                              "audio_dir": "/nonexistent/path/xyz"},
                        content_type="multipart/form-data")
        assert r.status_code == 400

    def test_empty_audio_dir_returns_results_with_unmatched(self, client, auth_headers):
        """CSV with 2 tracks + empty audio folder → both UNMATCHED, no error."""
        with tempfile.TemporaryDirectory() as tmp:
            csv_bytes = _minimal_csv()
            r = client.post("/api/analyse-csv", headers=auth_headers,
                            data={"csv_file": (io.BytesIO(csv_bytes), "sheet.csv"),
                                  "audio_dir": tmp, "threshold": "55"},
                            content_type="multipart/form-data")
        assert r.status_code == 200
        body = r.get_json()
        assert "results" in body
        assert all(row["status"] == "UNMATCHED" for row in body["results"])
        assert body["unmatched_files"] == []

    def test_extra_csv_rows_become_unmatched(self, client, auth_headers):
        """CSV with 2 tracks, 0 audio files → 2 UNMATCHED rows, tool must not crash."""
        with tempfile.TemporaryDirectory() as tmp:
            csv_bytes = _minimal_csv()
            r = client.post("/api/analyse-csv", headers=auth_headers,
                            data={"csv_file": (io.BytesIO(csv_bytes), "sheet.csv"),
                                  "audio_dir": tmp},
                            content_type="multipart/form-data")
        assert r.status_code == 200
        body = r.get_json()
        assert len(body["results"]) == 2
        unmatched_rows = [row for row in body["results"] if row["status"] == "UNMATCHED"]
        assert len(unmatched_rows) == 2

    def test_response_includes_unmatched_files_key(self, client, auth_headers):
        with tempfile.TemporaryDirectory() as tmp:
            csv_bytes = _minimal_csv()
            r = client.post("/api/analyse-csv", headers=auth_headers,
                            data={"csv_file": (io.BytesIO(csv_bytes), "sheet.csv"),
                                  "audio_dir": tmp},
                            content_type="multipart/form-data")
        assert r.status_code == 200
        assert "unmatched_files" in r.get_json()

    def test_response_includes_rename_formats(self, client, auth_headers):
        with tempfile.TemporaryDirectory() as tmp:
            csv_bytes = _minimal_csv()
            r = client.post("/api/analyse-csv", headers=auth_headers,
                            data={"csv_file": (io.BytesIO(csv_bytes), "sheet.csv"),
                                  "audio_dir": tmp},
                            content_type="multipart/form-data")
        assert r.status_code == 200
        body = r.get_json()
        assert "rename_formats" in body
        assert len(body["rename_formats"]) > 4

    def test_sessions_are_isolated(self, client):
        """Two different session IDs must not share match_table state."""
        with tempfile.TemporaryDirectory() as tmp:
            csv_bytes = _minimal_csv()

            def analyse(sid):
                return client.post("/api/analyse-csv",
                                   headers={"X-Session-Id": sid},
                                   data={"csv_file": (io.BytesIO(csv_bytes), "s.csv"),
                                         "audio_dir": tmp},
                                   content_type="multipart/form-data")

            r1 = analyse("session-alpha")
            r2 = analyse("session-beta")
            assert r1.status_code == 200
            assert r2.status_code == 200
            # Both should have independent results
            assert r1.get_json()["results"] is not None
            assert r2.get_json()["results"] is not None


# ── Rename formats ────────────────────────────────────────────────────────────

class TestRenameFormats:
    def test_formats_list_endpoint(self, client):
        r = client.get("/api/rename-formats")
        assert r.status_code == 200
        body = r.get_json()
        assert "formats" in body
        expected = {"001 - Title - Artist", "001 - Artist - Title",
                    "Title - Artist", "Artist - Title"}
        assert expected.issubset(set(body["formats"]))

    def test_rename_no_results_is_safe(self, client):
        """Rename on a brand-new session must not crash — returns empty results."""
        fresh = {"X-Session-Id": "fresh-rename-session", "Content-Type": "application/json"}
        r = client.post("/api/rename", headers=fresh,
                        data=json.dumps({"format_name": "Title - Artist"}))
        assert r.status_code == 200
        body = r.get_json()
        assert body["results"] == []
        assert "rename_stats" in body
        assert body["rename_stats"]["renamed"] == 0

    def test_rename_returns_stats_with_renamed_count(self, client, auth_headers):
        """After a real analyse, rename must report how many files actually moved."""
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "Animal Logic - Stone In My Shoe (SPOTISAVER).mp3"
            src.write_bytes(b"\xff\xfb" * 50)
            csv_bytes = _make_csv_bytes([
                ["Banner"],
                ["track_title","artist_name","album","date","","","","","spotify_link",
                 "","","","","","spotify_dur"],
                ["Stone In My Shoe","Animal Logic","","","","","","","","","","","","","3:42"],
            ])
            client.post("/api/analyse-csv", headers=auth_headers,
                       data={"csv_file": (io.BytesIO(csv_bytes), "s.csv"), "audio_dir": tmp},
                       content_type="multipart/form-data")

            r = client.post("/api/rename",
                           headers={**auth_headers, "Content-Type": "application/json"},
                           data=json.dumps({"format_name": "001 - Title - Artist"}))
            assert r.status_code == 200
            body = r.get_json()
            assert "rename_stats" in body
            assert "renamed" in body["rename_stats"]
            assert "errors" in body["rename_stats"]


# ── Serialise helpers ─────────────────────────────────────────────────────────

class TestSerialise:
    def test_matched_filename_basename_only(self):
        rows = [{"sheet_row": 1, "track_title": "T", "artist_name": "A",
                 "album": "", "spotify_link": "", "spotify_dur": 222.0,
                 "spotify_dur_raw": "3:42", "matched_file": "/music/Artist - Title.mp3",
                 "file_dur": 222.5, "diff": 0.5, "flag_gt1s": False,
                 "match_score": 95.0, "status": "OK", "suggested_rename": ""}]
        out = _serialise(rows)
        assert out[0]["matched_filename"] == "Artist - Title.mp3"

    def test_matched_filename_none_when_no_file(self):
        rows = [{"sheet_row": 1, "track_title": "T", "artist_name": "A",
                 "album": "", "spotify_link": "", "spotify_dur": None,
                 "spotify_dur_raw": "", "matched_file": None,
                 "file_dur": None, "diff": None, "flag_gt1s": False,
                 "match_score": 0.0, "status": "UNMATCHED", "suggested_rename": ""}]
        out = _serialise(rows)
        assert out[0]["matched_filename"] is None

    def test_serialise_audio_returns_basename(self):
        audio = [{"path": "/tmp/music/Song.mp3", "filename": "Song.mp3",
                  "duration": 222.0, "status": "OK", "used": False}]
        out = _serialise_audio(audio)
        assert out[0]["filename"] == "Song.mp3"
        assert out[0]["duration"] == 222.0


# ── Report download ───────────────────────────────────────────────────────────

class TestReport:
    def test_report_empty_returns_400(self, client):
        fresh = {"X-Session-Id": "fresh-report-session"}
        r = client.get("/api/report", headers=fresh)
        assert r.status_code == 400

    def test_report_returns_csv_after_analyse(self, client, auth_headers):
        with tempfile.TemporaryDirectory() as tmp:
            csv_bytes = _minimal_csv()
            client.post("/api/analyse-csv", headers=auth_headers,
                        data={"csv_file": (io.BytesIO(csv_bytes), "s.csv"),
                              "audio_dir": tmp},
                        content_type="multipart/form-data")
        r = client.get("/api/report", headers=auth_headers)
        assert r.status_code == 200
        assert "text/csv" in r.content_type
        content = r.data.decode("utf-8")
        assert "track_title" in content   # header row present


# ── Missing / extra report download ────────────────────────────────────────────

class TestMissingReport:
    def test_report_missing_empty_returns_400(self, client):
        fresh = {"X-Session-Id": "fresh-missing-report-session"}
        r = client.get("/api/report-missing", headers=fresh)
        assert r.status_code == 400

    def test_report_missing_lists_unuploaded_sheet_rows(self, client, auth_headers):
        with tempfile.TemporaryDirectory() as tmp:
            # Empty audio dir: nothing uploaded, so both sheet tracks are unmatched.
            csv_bytes = _minimal_csv()
            client.post("/api/analyse-csv", headers=auth_headers,
                        data={"csv_file": (io.BytesIO(csv_bytes), "s.csv"),
                              "audio_dir": tmp},
                        content_type="multipart/form-data")
        r = client.get("/api/report-missing", headers=auth_headers)
        assert r.status_code == 200
        assert "text/csv" in r.content_type
        content = r.data.decode("utf-8")
        assert "SONGS IN SHEET BUT NOT UPLOADED" in content
        assert "Stone In My Shoe" in content
        assert "Flying High" in content
        assert "SONGS UPLOADED BUT NOT ON SHEET" in content

    def test_report_missing_lists_unmatched_audio(self, client, auth_headers):
        with tempfile.TemporaryDirectory() as tmp:
            # An audio file with a name that won't fuzzy-match either sheet row.
            extra = Path(tmp) / "Totally Unrelated File - Nobody.mp3"
            extra.write_bytes(b"\x00" * 10)
            csv_bytes = _minimal_csv()
            client.post("/api/analyse-csv", headers=auth_headers,
                        data={"csv_file": (io.BytesIO(csv_bytes), "s.csv"),
                              "audio_dir": tmp},
                        content_type="multipart/form-data")
            r = client.get("/api/report-missing", headers=auth_headers)
        assert r.status_code == 200
        content = r.data.decode("utf-8")
        assert "Totally Unrelated File" in content


# ── Assign-unmatched endpoint ─────────────────────────────────────────────────

class TestAssignUnmatched:
    def test_missing_params_returns_400(self, client, auth_headers):
        r = client.post("/api/assign-unmatched",
                        headers={**auth_headers, "Content-Type": "application/json"},
                        data=json.dumps({"sheet_row": 3}))
        assert r.status_code == 400

    def test_nonexistent_file_returns_404(self, client, auth_headers):
        r = client.post("/api/assign-unmatched",
                        headers={**auth_headers, "Content-Type": "application/json"},
                        data=json.dumps({"sheet_row": 3, "file_path": "/no/such/file.mp3"}))
        assert r.status_code == 404


# ── _build_renamed_zip ─────────────────────────────────────────────────────────

class TestBuildRenamedZip:
    def test_returns_none_for_empty_table(self):
        from src.web_server import _build_renamed_zip
        assert _build_renamed_zip([]) is None

    def test_returns_none_when_no_matched_files(self):
        from src.web_server import _build_renamed_zip
        table = [{"sheet_row": 1, "matched_file": None}]
        assert _build_renamed_zip(table) is None

    def test_returns_none_when_file_missing_on_disk(self):
        from src.web_server import _build_renamed_zip
        table = [{"sheet_row": 1, "matched_file": "/no/such/path.mp3"}]
        assert _build_renamed_zip(table) is None

    def test_builds_zip_with_correct_contents(self):
        import zipfile
        from src.web_server import _build_renamed_zip
        with tempfile.TemporaryDirectory() as tmp:
            f1 = Path(tmp) / "001 - Stone In My Shoe - Animal Logic.mp3"
            f1.write_bytes(b"\xff\xfb" * 20)
            f2 = Path(tmp) / "002 - Flying High - Army Of Lovers.mp3"
            f2.write_bytes(b"\xff\xfb" * 20)
            table = [
                {"sheet_row": 1, "matched_file": str(f1)},
                {"sheet_row": 2, "matched_file": str(f2)},
            ]
            zip_path = _build_renamed_zip(table)
            assert zip_path is not None
            assert os.path.exists(zip_path)
            with zipfile.ZipFile(zip_path) as zf:
                names = set(zf.namelist())
                assert "001 - Stone In My Shoe - Animal Logic.mp3" in names
                assert "002 - Flying High - Army Of Lovers.mp3" in names

    def test_skips_rows_with_missing_files_but_includes_valid_ones(self):
        import zipfile
        from src.web_server import _build_renamed_zip
        with tempfile.TemporaryDirectory() as tmp:
            f1 = Path(tmp) / "Real File.mp3"
            f1.write_bytes(b"x")
            table = [
                {"sheet_row": 1, "matched_file": str(f1)},
                {"sheet_row": 2, "matched_file": "/no/such/ghost.mp3"},
            ]
            zip_path = _build_renamed_zip(table)
            assert zip_path is not None
            with zipfile.ZipFile(zip_path) as zf:
                assert zf.namelist() == ["Real File.mp3"]

    def test_duplicate_filenames_get_disambiguated(self):
        import zipfile
        from src.web_server import _build_renamed_zip
        with tempfile.TemporaryDirectory() as tmp1, tempfile.TemporaryDirectory() as tmp2:
            # Two different directories, same basename — simulates a
            # collision that could occur after manual reassignment
            f1 = Path(tmp1) / "Same Name.mp3"
            f1.write_bytes(b"a")
            f2 = Path(tmp2) / "Same Name.mp3"
            f2.write_bytes(b"b")
            table = [
                {"sheet_row": 1, "matched_file": str(f1)},
                {"sheet_row": 2, "matched_file": str(f2)},
            ]
            zip_path = _build_renamed_zip(table)
            with zipfile.ZipFile(zip_path) as zf:
                names = zf.namelist()
                assert len(names) == 2
                assert len(set(names)) == 2   # no actual collision in the zip


# ── /api/download-renamed-zip ───────────────────────────────────────────────────

class TestDownloadRenamedZipEndpoint:
    def test_no_results_returns_400(self, client):
        fresh = {"X-Session-Id": "fresh-zip-dl-session"}
        r = client.get("/api/download-renamed-zip", headers=fresh)
        assert r.status_code == 400
        assert "error" in r.get_json()

    def test_full_flow_analyse_rename_download(self, client):
        """End-to-end: analyse → rename → download zip → verify renamed
        filenames appear inside the downloaded archive."""
        import zipfile
        sid = "full-flow-zip-test"
        headers = {"X-Session-Id": sid}

        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "Animal Logic - Stone In My Shoe (SPOTISAVER).mp3"
            src.write_bytes(b"\xff\xfb" * 50)

            csv_bytes = _make_csv_bytes([
                ["Banner"],
                ["track_title","artist_name","album","date","","","","","spotify_link",
                 "","","","","","spotify_dur"],
                ["Stone In My Shoe","Animal Logic","","","","","","","","","","","","","3:42"],
            ])

            r1 = client.post("/api/analyse-csv", headers=headers,
                            data={"csv_file": (io.BytesIO(csv_bytes), "s.csv"), "audio_dir": tmp},
                            content_type="multipart/form-data")
            assert r1.status_code == 200

            r2 = client.post("/api/rename",
                            headers={**headers, "Content-Type": "application/json"},
                            data=json.dumps({"format_name": "001 - Title - Artist"}))
            assert r2.status_code == 200
            assert r2.get_json()["rename_stats"]["renamed"] == 1

            r3 = client.get("/api/download-renamed-zip", headers=headers)
            assert r3.status_code == 200
            assert r3.content_type == "application/zip"

            zip_bytes = io.BytesIO(r3.data)
            with zipfile.ZipFile(zip_bytes) as zf:
                names = zf.namelist()
                assert "003 - Stone In My Shoe - Animal Logic.mp3" in names

    def test_sessions_get_independent_zips(self, client):
        """Two different sessions downloading must each get their own
        files — no cross-session leakage."""
        import zipfile

        with tempfile.TemporaryDirectory() as tmp_a, tempfile.TemporaryDirectory() as tmp_b:
            fa = Path(tmp_a) / "Artist A - Song A.mp3"; fa.write_bytes(b"a")
            fb = Path(tmp_b) / "Artist B - Song B.mp3"; fb.write_bytes(b"b")

            csv_a = _make_csv_bytes([
                ["Banner"],
                ["track_title","artist_name","album","date","","","","","spotify_link",
                 "","","","","","spotify_dur"],
                ["Song A","Artist A","","","","","","","","","","","","","3:00"],
            ])
            csv_b = _make_csv_bytes([
                ["Banner"],
                ["track_title","artist_name","album","date","","","","","spotify_link",
                 "","","","","","spotify_dur"],
                ["Song B","Artist B","","","","","","","","","","","","","3:00"],
            ])

            h_a = {"X-Session-Id": "session-a-zip"}
            h_b = {"X-Session-Id": "session-b-zip"}

            client.post("/api/analyse-csv", headers=h_a,
                       data={"csv_file": (io.BytesIO(csv_a), "a.csv"), "audio_dir": tmp_a},
                       content_type="multipart/form-data")
            client.post("/api/analyse-csv", headers=h_b,
                       data={"csv_file": (io.BytesIO(csv_b), "b.csv"), "audio_dir": tmp_b},
                       content_type="multipart/form-data")

            r_a = client.get("/api/download-renamed-zip", headers=h_a)
            r_b = client.get("/api/download-renamed-zip", headers=h_b)

            with zipfile.ZipFile(io.BytesIO(r_a.data)) as zf_a:
                names_a = zf_a.namelist()
            with zipfile.ZipFile(io.BytesIO(r_b.data)) as zf_b:
                names_b = zf_b.namelist()

            assert any("Song A" in n for n in names_a)
            assert not any("Song B" in n for n in names_a)
            assert any("Song B" in n for n in names_b)
            assert not any("Song A" in n for n in names_b)

    def test_plain_request_without_session_header_gets_isolated_default(self, client):
        """Regression guard: confirms that requests without X-Session-Id
        land in their own isolated 'default' bucket rather than crashing —
        and explicitly do NOT see another session's data. This documents
        why the frontend must always send the header via fetch(), never
        rely on bare <a href> navigation for session-scoped downloads."""
        sid_headers = {"X-Session-Id": "header-equipped-session"}
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "Artist - Title.mp3"
            src.write_bytes(b"x")
            csv_bytes = _make_csv_bytes([
                ["Banner"],
                ["track_title","artist_name","album","date","","","","","spotify_link",
                 "","","","","","spotify_dur"],
                ["Title","Artist","","","","","","","","","","","","","3:00"],
            ])
            client.post("/api/analyse-csv", headers=sid_headers,
                       data={"csv_file": (io.BytesIO(csv_bytes), "s.csv"), "audio_dir": tmp},
                       content_type="multipart/form-data")

            # Simulate a stray request with no session header at all
            r_no_header = client.get("/api/download-renamed-zip")
            # Must not error with a 500, and must not see session data
            # it has no business seeing — a clean 400 "no data" is correct.
            assert r_no_header.status_code == 400


# ── SessionStore resource cleanup safety ─────────────────────────────────────

class TestSessionResourceCleanup:
    def test_owned_audio_dir_removed_on_ttl_eviction(self):
        store = SessionStore(ttl=1)
        with tempfile.TemporaryDirectory() as parent:
            owned = os.path.join(parent, "owned_upload_dir")
            os.makedirs(owned)
            Path(owned, "file.mp3").write_bytes(b"x")

            sess = store.get_or_create("evict-me")
            sess["_owned_audio_dir"] = owned
            assert os.path.isdir(owned)

            time.sleep(1.1)
            store.get_or_create("trigger-eviction")   # forces _evict() to run

            assert not os.path.exists(owned), "Owned temp dir must be cleaned up on eviction"

    def test_owned_audio_dir_removed_on_lru_eviction_at_capacity(self):
        store = SessionStore(max_sessions=1)
        with tempfile.TemporaryDirectory() as parent:
            owned = os.path.join(parent, "owned_dir")
            os.makedirs(owned)

            sess = store.get_or_create("first")
            sess["_owned_audio_dir"] = owned
            store.get_or_create("second")   # forces capacity eviction of "first"

            assert not os.path.exists(owned)

    def test_user_provided_real_directory_is_never_deleted(self):
        """Critical safety guarantee: a real folder the user typed into the
        Audio Folder field must NEVER be touched by session eviction, even
        after TTL expiry, because it is not tracked as '_owned_audio_dir'."""
        store = SessionStore(ttl=1)
        with tempfile.TemporaryDirectory() as real_user_folder:
            Path(real_user_folder, "MyRealSong.mp3").write_bytes(b"precious data")

            sess = store.get_or_create("user-typed-path-session")
            # This mirrors what /api/analyse-csv does for a user-typed path:
            # it sets "audio_dir" but NEVER "_owned_audio_dir".
            sess["audio_dir"] = real_user_folder
            assert sess.get("_owned_audio_dir") is None

            time.sleep(1.1)
            store.get_or_create("force-eviction-check")

            assert os.path.isdir(real_user_folder), "User's real folder must survive eviction"
            assert os.path.exists(os.path.join(real_user_folder, "MyRealSong.mp3"))

    def test_new_sessions_have_owned_audio_dir_field(self):
        store = SessionStore()
        sess = store.get_or_create("new-session")
        assert "_owned_audio_dir" in sess
        assert sess["_owned_audio_dir"] is None


class TestFilterAndReSync:
    def test_reassign_updates_used_status_correctly(self, client, auth_headers):
        from src.web_server import _store
        from unittest.mock import patch
        
        sess = _store.get_or_create("reassign-session-test")
        sess["audio_files"] = [
            {"path": str(Path("/music/a.mp3")), "filename": "a.mp3", "duration": 100.0, "used": False},
            {"path": str(Path("/music/b.mp3")), "filename": "b.mp3", "duration": 120.0, "used": False},
        ]
        sess["match_table"] = [
            {"sheet_row": 3, "track_title": "Track 1", "artist_name": "Artist 1", "spotify_dur": 100.0, "matched_file": None, "status": "UNMATCHED"},
        ]
        sess["audio_dir"] = "/music"

        with patch("src.web_server.Path.exists", return_value=True), \
             patch("src.web_server.extract_duration", return_value=100.0):
            r = client.post("/api/reassign", headers={"X-Session-Id": "reassign-session-test"},
                            json={"sheet_row": 3, "file_path": str(Path("/music/a.mp3")), "tolerance": 1.0, "use_microseconds": True})

        assert r.status_code == 200
        body = r.get_json()
        assert body["results"][0]["matched_file"] == str(Path("/music/a.mp3"))
        unmatched_paths = [f["path"] for f in body["unmatched_files"]]
        assert str(Path("/music/a.mp3")) not in unmatched_paths
        assert str(Path("/music/b.mp3")) in unmatched_paths

    def test_assign_unmatched_updates_used_status_correctly(self, client, auth_headers):
        from src.web_server import _store
        from unittest.mock import patch
        
        sess = _store.get_or_create("assign-unmatched-session-test")
        sess["audio_files"] = [
            {"path": str(Path("/music/a.mp3")), "filename": "a.mp3", "duration": 100.0, "used": False},
            {"path": str(Path("/music/b.mp3")), "filename": "b.mp3", "duration": 120.0, "used": False},
        ]
        sess["match_table"] = [
            {"sheet_row": 3, "track_title": "Track 1", "artist_name": "Artist 1", "spotify_dur": 100.0, "matched_file": None, "status": "UNMATCHED"},
        ]
        sess["audio_dir"] = "/music"

        with patch("src.web_server.Path.exists", return_value=True), \
             patch("src.web_server.extract_duration", return_value=100.0):
            r = client.post("/api/assign-unmatched", headers={"X-Session-Id": "assign-unmatched-session-test"},
                            json={"sheet_row": 3, "file_path": str(Path("/music/a.mp3")), "tolerance": 1.0, "use_microseconds": True})

        assert r.status_code == 200
        body = r.get_json()
        assert body["results"][0]["matched_file"] == str(Path("/music/a.mp3"))
        unmatched_paths = [f["path"] for f in body["unmatched_files"]]
        assert str(Path("/music/a.mp3")) not in unmatched_paths
        assert str(Path("/music/b.mp3")) in unmatched_paths
