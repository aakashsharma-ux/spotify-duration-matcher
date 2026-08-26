"""Flask web server — multi-user session-based architecture.

Each browser tab gets a UUID session token (stored in localStorage, sent as
the X-Session-Id request header). Sessions are stored in a thread-safe
in-memory store with a 1-hour TTL, supporting 80-90 concurrent users.
"""

from __future__ import annotations

import io
import logging
import os
import shutil
import tempfile
import threading
import time
import zipfile
from collections import OrderedDict
from datetime import datetime
from functools import wraps
from pathlib import Path
from typing import Any, Optional

from flask import Flask, after_this_request, jsonify, render_template, request, send_file

from .audio_scanner import AUDIO_EXTENSIONS, scan_audio_files, extract_duration
from .matcher import match_tracks, compute_unmatched_status
from .renamer import rename_matched_files
from .reporter import export_csv_report, _row_to_csv_dict, CSV_COLUMNS, _write_missing_sections
from .sheet_loader import load_sheet

logger = logging.getLogger(__name__)

MAX_UPLOAD_MB     = 150        # per REQUEST ceiling — the browser splits large
                                # audio selections into small chunked requests
                                # (see templates/index.html), so this only needs
                                # to comfortably cover one chunk, not everything
                                # at once. Keeps a single bad request from ever
                                # trying to buffer close to this box's 512MB RAM.
SESSION_TTL_SEC   = 3600       # 1 hour idle timeout
MAX_SESSIONS      = 300        # upper bound for memory safety
API               = "/api"

# ── Thread-safe session store ──────────────────────────────────────────────

class SessionStore:
    """LRU session store with TTL eviction; fully thread-safe.

    Sessions can own server-created temp directories (e.g. uploaded audio
    files, generated download zips). These are tracked separately from
    user-supplied real paths so eviction never deletes a user's own folder.
    """

    def __init__(self, max_sessions: int = MAX_SESSIONS, ttl: int = SESSION_TTL_SEC) -> None:
        self._store: OrderedDict[str, dict] = OrderedDict()
        self._lock  = threading.Lock()
        self._max   = max_sessions
        self._ttl   = ttl

    @staticmethod
    def _cleanup_resources(sess: dict) -> None:
        """Remove server-owned temp directories tied to *sess*.

        Only ever removes directories explicitly tracked under
        "_owned_audio_dir" (set by the upload-audio endpoint, never by a
        user-typed folder path), so a real personal music folder can never
        be deleted by eviction.
        """
        owned_dir = sess.get("_owned_audio_dir")
        if owned_dir:
            shutil.rmtree(owned_dir, ignore_errors=True)

    def _evict(self) -> None:
        """Remove expired sessions and clean up their temp resources.

        Must be called under lock.
        """
        cutoff  = time.monotonic() - self._ttl
        expired = [sid for sid, v in self._store.items() if v["ts"] < cutoff]
        for sid in expired:
            self._cleanup_resources(self._store[sid])
            del self._store[sid]

    def get_or_create(self, session_id: str) -> dict:
        """Return the session data dict for *session_id*, creating if absent."""
        with self._lock:
            self._evict()
            if session_id not in self._store:
                if len(self._store) >= self._max:
                    _, evicted = self._store.popitem(last=False)   # evict LRU
                    self._cleanup_resources(evicted)
                self._store[session_id] = {
                    "ts":               time.monotonic(),
                    "match_table":      [],
                    "audio_files":      [],
                    "audio_dir":        "",
                    "gsheet_obj":       None,
                    "_owned_audio_dir": None,
                }
            else:
                self._store[session_id]["ts"] = time.monotonic()
                self._store.move_to_end(session_id)
            return self._store[session_id]

    @property
    def active_count(self) -> int:
        with self._lock:
            self._evict()
            return len(self._store)


_store = SessionStore()


def _session(fn):
    """Decorator that injects the caller's session dict as ``sess``."""
    @wraps(fn)
    def wrapper(*args, **kwargs):
        sid = request.headers.get("X-Session-Id", "default")
        kwargs["sess"] = _store.get_or_create(sid)
        return fn(*args, **kwargs)
    return wrapper


# ── Rename format registry ─────────────────────────────────────────────────

RENAME_FORMATS: dict[str, str] = {
    "001 - Title - Artist": "{idx:03d} - {title} - {artist}{ext}",
    "001 - Artist - Title": "{idx:03d} - {artist} - {title}{ext}",
    "Title - Artist":       "{title} - {artist}{ext}",
    "Artist - Title":       "{artist} - {title}{ext}",
    "001 - Title":          "{idx:03d} - {title}{ext}",
    "001 - Artist":         "{idx:03d} - {artist}{ext}",
    "Title":                "{title}{ext}",
    "Artist - Title (no serial)": "{artist} - {title}{ext}",
}


def create_app(config: dict) -> Flask:
    """Create and configure the Flask application."""
    app = Flask(
        __name__,
        template_folder=str(Path(__file__).parent.parent / "templates"),
    )
    app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_MB * 1024 * 1024

    # ── JSON error handlers ───────────────────────────────────────────────
    # Flask/Werkzeug's default error pages are HTML. Every /api/* response
    # is expected to be JSON by the frontend — without these handlers, an
    # oversized or failed request returns an HTML page, the browser's
    # JSON.parse throws inside an event-handler callback with nothing to
    # catch it, and the upload promise never resolves *or* rejects. That's
    # what "hangs forever" actually was: not merely slow, but stuck.
    @app.errorhandler(413)
    def _too_large(_exc):
        return jsonify({
            "error": f"That batch is too large for one request (over "
                     f"{MAX_UPLOAD_MB} MB). Files upload in small chunks "
                     f"automatically — please try again."
        }), 413

    @app.errorhandler(500)
    def _server_error(exc):
        logger.exception("Unhandled server error: %s", exc)
        return jsonify({"error": "Server error — please try again."}), 500

    # ── Helpers ─────────────────────────────────────────────────────────────

    def _cfg(key: str, default: Any = None) -> Any:
        return config.get(key, default)

    def _run_analysis(
        sheet_rows: list[dict],
        audio_dir: Path,
        recursive: bool,
        threshold: int,
        tolerance: float,
        use_microseconds: bool,
        sess: dict,
    ) -> dict:
        """Core analysis pipeline; updates *sess* and returns JSON payload."""
        audio_files = scan_audio_files(
            audio_dir    = audio_dir,
            recursive    = recursive,
            cache_db_name= _cfg("cache_db", ".duration_cache.db"),
        )
        match_table, unmatched_audio = match_tracks(
            sheet_rows      = sheet_rows,
            audio_files     = audio_files,
            threshold       = threshold,
            tolerance       = tolerance,
            use_microseconds= use_microseconds,
        )
        sess["match_table"] = match_table
        sess["audio_files"] = audio_files
        sess["audio_dir"]   = str(audio_dir)
        return {
            "results":          _serialise(match_table),
            "unmatched_files":  _serialise_audio(unmatched_audio),
            "rename_formats":   list(RENAME_FORMATS.keys()),
        }

    # ── Routes ───────────────────────────────────────────────────────────────

    @app.route("/")
    def index() -> Any:
        return render_template("index.html")

    @app.route("/static/sunset.jpg")
    def sunset_img() -> Any:
        return send_file(str(Path(__file__).parent.parent / "static" / "sunset.jpg"))

    @app.after_request
    def add_header(response: Any) -> Any:
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        return response

    @app.route(f"{API}/health")
    def health() -> Any:
        return jsonify({"status": "ok", "active_sessions": _store.active_count})

    # ── Upload audio files ────────────────────────────────────────────────────

    @app.route(f"{API}/upload-audio", methods=["POST"])
    @_session
    def upload_audio(sess: dict) -> Any:
        """Save one chunk of an audio selection.

        The browser splits large selections into several small requests so
        one slow or failed chunk never means re-uploading everything (see
        the upload logic in templates/index.html). The FIRST chunk of a
        new selection is sent with reset=true, which clears out any
        previous upload owned by this session; every following chunk in
        the same selection appends into that same directory. The response
        always reports the running total on disk, so the client has an
        authoritative count even across many chunked requests.
        """
        files = request.files.getlist("audio_files")
        if not files or all(f.filename == "" for f in files):
            return jsonify({"error": "No audio files received."}), 400

        reset   = request.form.get("reset", "false").lower() == "true"
        tmp_dir = sess.get("_owned_audio_dir")
        if reset or not tmp_dir or not os.path.isdir(tmp_dir):
            if tmp_dir:
                shutil.rmtree(tmp_dir, ignore_errors=True)   # drop the old batch, don't leak it
            tmp_dir = tempfile.mkdtemp(prefix="sdm_audio_")

        saved = 0
        for f in files:
            safe_name = os.path.basename(f.filename.replace("\\", "/"))
            if safe_name:
                f.save(os.path.join(tmp_dir, safe_name))
                saved += 1

        sess["audio_dir"] = tmp_dir
        sess["_owned_audio_dir"] = tmp_dir   # safe to delete on eviction — we created it
        total = sum(
            1 for entry in os.scandir(tmp_dir)
            if entry.is_file() and os.path.splitext(entry.name)[1].lower() in AUDIO_EXTENSIONS
        )
        logger.info("Uploaded %d audio file(s) to %s (running total: %d)", saved, tmp_dir, total)
        return jsonify({"audio_dir": tmp_dir, "file_count": total, "chunk_file_count": saved})

    # ── Analyse via CSV upload ─────────────────────────────────────────────────

    @app.route(f"{API}/analyse-csv", methods=["POST"])
    @_session
    def analyse_csv(sess: dict) -> Any:
        if "csv_file" not in request.files:
            return jsonify({"error": "No csv_file in request."}), 400
        csv_file = request.files["csv_file"]
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".csv", mode="wb")
        csv_file.save(tmp.name)

        audio_dir_str  = request.form.get("audio_dir", "").strip() or sess.get("audio_dir", "")
        recursive      = request.form.get("recursive", "true").lower() == "true"
        threshold      = int(request.form.get("threshold", _cfg("match_threshold", 55)))
        tolerance      = float(request.form.get("tolerance", _cfg("duration_tolerance_sec", 1.0)))
        use_microsec   = request.form.get("use_microseconds", "true").lower() == "true"

        if not audio_dir_str:
            return jsonify({"error": "Provide audio_dir or upload audio files first."}), 400
        audio_dir = Path(audio_dir_str)
        if not audio_dir.exists():
            return jsonify({"error": f"audio_dir does not exist: {audio_dir}"}), 400
        try:
            sheet_rows, _ = load_sheet(csv_path=tmp.name, sheet_id=None)
        except Exception as exc:
            return jsonify({"error": f"CSV parse failed: {exc}"}), 500
        finally:
            try: os.unlink(tmp.name)
            except OSError: pass
        if not sheet_rows:
            return jsonify({"error": "Sheet has no data rows."}), 400
        return jsonify(_run_analysis(sheet_rows, audio_dir, recursive,
                                     threshold, tolerance, use_microsec, sess))

    # ── Analyse via Sheet ID ──────────────────────────────────────────────────

    @app.route(f"{API}/analyse", methods=["POST"])
    @_session
    def analyse(sess: dict) -> Any:
        data           = request.get_json(force=True, silent=True) or {}
        sheet_id       = data.get("sheet_id", "").strip()
        audio_dir_str  = data.get("audio_dir", "").strip() or sess.get("audio_dir", "")
        recursive      = data.get("recursive", True)
        threshold      = int(data.get("threshold", _cfg("match_threshold", 55)))
        tolerance      = float(data.get("tolerance", _cfg("duration_tolerance_sec", 1.0)))
        use_microsec   = data.get("use_microseconds", True)
        creds_file     = _cfg("credentials_file", "creds.json")
        if not sheet_id:
            return jsonify({"error": "Provide sheet_id."}), 400
        if not audio_dir_str:
            return jsonify({"error": "Provide audio_dir or upload audio files first."}), 400
        audio_dir = Path(audio_dir_str)
        if not audio_dir.exists():
            return jsonify({"error": f"audio_dir does not exist: {audio_dir}"}), 400
        try:
            sheet_rows, gsheet_obj = load_sheet(csv_path=None, sheet_id=sheet_id, creds_file=creds_file)
            sess["gsheet_obj"] = gsheet_obj
        except Exception as exc:
            return jsonify({"error": f"Sheet load failed: {exc}"}), 500
        if not sheet_rows:
            return jsonify({"error": "Sheet has no data rows."}), 400
        return jsonify(_run_analysis(sheet_rows, audio_dir, recursive,
                                     threshold, tolerance, use_microsec, sess))

    # ── Reassign ──────────────────────────────────────────────────────────────

    @app.route(f"{API}/reassign", methods=["POST"])
    @_session
    def reassign(sess: dict) -> Any:
        data          = request.get_json(force=True, silent=True) or {}
        sheet_row     = data.get("sheet_row")
        new_file_path = data.get("file_path", "").strip()
        tolerance     = float(data.get("tolerance", _cfg("duration_tolerance_sec", 1.0)))
        use_microsec  = data.get("use_microseconds", True)
        if sheet_row is None or not new_file_path:
            return jsonify({"error": "sheet_row and file_path are required."}), 400
        new_path = Path(new_file_path)
        if not new_path.exists():
            return jsonify({"error": f"File not found: {new_file_path}"}), 404

        match_table = sess.get("match_table", [])
        dur         = extract_duration(new_path)
        for row in match_table:
            if row["sheet_row"] == sheet_row:
                spotify_dur = row.get("spotify_dur")
                diff = round(dur - spotify_dur, 2) if (dur is not None and spotify_dur is not None) else None
                import math as _math
                if use_microsec:
                    flagged = abs(diff) > tolerance if diff is not None else False
                else:
                    flagged = abs(_math.floor(dur or 0) - _math.floor(spotify_dur or 0)) > tolerance if (dur and spotify_dur) else False
                row.update({
                    "matched_file": str(new_path),
                    "file_dur":     dur,
                    "diff":         diff,
                    "flag_gt1s":    flagged,
                    "status":       "MANUAL",
                    "match_score":  0.0,
                })
                break
        sess["match_table"] = match_table

        audio_files = sess.get("audio_files", [])
        matched_paths = {r["matched_file"] for r in match_table if r.get("matched_file")}
        for af in audio_files:
            af["used"] = af["path"] in matched_paths
        sess["audio_files"] = audio_files

        unmatched_audio = [f for f in audio_files if not f.get("used")]
        unmatched_audio = compute_unmatched_status(unmatched_audio, match_table)

        return jsonify({
            "results":         _serialise(match_table),
            "unmatched_files": _serialise_audio(unmatched_audio),
        })

    # ── Assign unmatched audio file to a sheet row ────────────────────────────

    @app.route(f"{API}/assign-unmatched", methods=["POST"])
    @_session
    def assign_unmatched(sess: dict) -> Any:
        data          = request.get_json(force=True, silent=True) or {}
        sheet_row     = data.get("sheet_row")
        file_path     = data.get("file_path", "").strip()
        tolerance     = float(data.get("tolerance", _cfg("duration_tolerance_sec", 1.0)))
        use_microsec  = data.get("use_microseconds", True)
        if sheet_row is None or not file_path:
            return jsonify({"error": "sheet_row and file_path are required."}), 400
        new_path = Path(file_path)
        if not new_path.exists():
            return jsonify({"error": f"File not found: {file_path}"}), 404

        match_table  = sess.get("match_table", [])
        audio_files  = sess.get("audio_files", [])
        dur          = extract_duration(new_path)
        import math as _math

        for row in match_table:
            if row["sheet_row"] == sheet_row:
                spotify_dur = row.get("spotify_dur")
                diff = round(dur - spotify_dur, 2) if (dur is not None and spotify_dur is not None) else None
                if use_microsec:
                    flagged = abs(diff) > tolerance if diff is not None else False
                else:
                    flagged = abs(_math.floor(dur or 0) - _math.floor(spotify_dur or 0)) > tolerance if (dur and spotify_dur) else False
                row.update({
                    "matched_file": str(new_path),
                    "file_dur":     dur,
                    "diff":         diff,
                    "flag_gt1s":    flagged,
                    "status":       "MANUAL",
                    "match_score":  0.0,
                })
                break

        sess["match_table"] = match_table

        matched_paths = {r["matched_file"] for r in match_table if r.get("matched_file")}
        for af in audio_files:
            af["used"] = af["path"] in matched_paths
        sess["audio_files"] = audio_files

        unmatched_audio = [f for f in audio_files if not f.get("used")]
        unmatched_audio = compute_unmatched_status(unmatched_audio, match_table)

        return jsonify({
            "results":         _serialise(match_table),
            "unmatched_files": _serialise_audio(unmatched_audio),
        })

    # ── Rename ────────────────────────────────────────────────────────────────

    @app.route(f"{API}/rename", methods=["POST"])
    @_session
    def rename(sess: dict) -> Any:
        data        = request.get_json(force=True, silent=True) or {}
        format_name = data.get("format_name", "")
        custom_tpl  = data.get("custom_template", "")
        template    = (
            custom_tpl
            or RENAME_FORMATS.get(format_name)
            or _cfg("rename_template", "{idx:03d} - {title} - {artist}{ext}")
        )
        match_table = sess.get("match_table", [])
        if not match_table:
            return jsonify({
                "results": [], "unmatched_files": [],
                "rename_stats": {"renamed": 0, "skipped_same_name": 0,
                                  "skipped_not_found": 0, "skipped_dest_exists": 0,
                                  "errors": []},
            })

        # Save mapping of old file paths by sheet_row to update audio_files after renaming
        old_matched_files = {r["sheet_row"]: r.get("matched_file") for r in match_table if r.get("matched_file")}

        match_table, stats = rename_matched_files(match_table, template=template)
        sess["match_table"] = match_table

        audio_files = sess.get("audio_files", [])
        for row in match_table:
            sheet_row = row.get("sheet_row")
            old_path = old_matched_files.get(sheet_row)
            new_path = row.get("matched_file")
            if old_path and new_path and old_path != new_path:
                for af in audio_files:
                    if af["path"] == old_path:
                        af["path"] = new_path
                        af["filename"] = os.path.basename(new_path)
                        break

        matched_paths = {r["matched_file"] for r in match_table if r.get("matched_file")}
        for af in audio_files:
            af["used"] = af["path"] in matched_paths
        sess["audio_files"] = audio_files

        unmatched_audio = [f for f in audio_files if not f.get("used")]
        unmatched_audio = compute_unmatched_status(unmatched_audio, match_table)

        return jsonify({
            "results":         _serialise(match_table),
            "unmatched_files": _serialise_audio(unmatched_audio),
            "rename_stats":    stats,
        })

    # ── Download renamed files as a zip ───────────────────────────────────────

    @app.route(f"{API}/download-renamed-zip", methods=["GET"])
    @_session
    def download_renamed_zip(sess: dict) -> Any:
        """Stream a zip of every currently matched audio file for download.

        Filenames inside the zip reflect each file's CURRENT on-disk name —
        i.e. the renamed name if Rename has been run, or the original name
        otherwise. This is the primary way users get their files back when
        the audio was supplied via browser upload (and therefore lives in a
        server-side temp folder they cannot otherwise reach).
        """
        match_table = sess.get("match_table", [])
        zip_path = _build_renamed_zip(match_table)
        if zip_path is None:
            return jsonify({
                "error": "No matched audio files available. Run Analyse (and Rename) first."
            }), 400

        zip_dir = os.path.dirname(zip_path)

        @after_this_request
        def _cleanup(response):
            shutil.rmtree(zip_dir, ignore_errors=True)
            return response

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        return send_file(
            zip_path,
            mimetype="application/zip",
            as_attachment=True,
            download_name=f"renamed_audio_{ts}.zip",
        )



    @app.route(f"{API}/candidates", methods=["POST"])
    @_session
    def candidates(sess: dict) -> Any:
        data      = request.get_json(force=True, silent=True) or {}
        sheet_row = data.get("sheet_row")
        top_n     = int(data.get("top_n", 5))
        match_table = sess.get("match_table", [])
        audio_dir   = Path(sess.get("audio_dir", "."))
        target = next((r for r in match_table if r["sheet_row"] == sheet_row), None)
        if target is None:
            return jsonify({"error": "Row not found."}), 404
        from rapidfuzz import fuzz
        from .filename_parser import normalise, parse_filename
        import re as _re
        title  = normalise(target.get("track_title", ""))
        artist = normalise(target.get("artist_name", ""))
        scored = []
        for af in scan_audio_files(audio_dir=audio_dir, recursive=True):
            best = 0.0
            filename = Path(af["path"]).name
            m = _re.match(r"^(\d{1,3})[\.\s\-]+", filename)
            file_track_num = int(m.group(1)) if m else None
            for _, art_c, tit_c in parse_filename(af["path"]):
                t = fuzz.token_sort_ratio(normalise(tit_c), title)
                a = fuzz.token_sort_ratio(normalise(art_c), artist)
                score = 0.6 * t + 0.4 * a
                if file_track_num == sheet_row and score >= 30.0:
                    score = 100.0
                best = max(best, score)
            scored.append({"path": af["path"], "score": round(best, 1), "duration": af["duration"]})
        scored.sort(key=lambda x: x["score"], reverse=True)
        return jsonify({"candidates": scored[:top_n]})

    # ── Download report ───────────────────────────────────────────────────────

    @app.route(f"{API}/report", methods=["GET"])
    @_session
    def download_report(sess: dict) -> Any:
        match_table = sess.get("match_table", [])
        if not match_table:
            return jsonify({"error": "No results yet. Run Analyse first."}), 400
        import csv
        output = io.StringIO()
        writer = csv.DictWriter(output, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        for i, row in enumerate(match_table):
            writer.writerow(_row_to_csv_dict(row, i + 1))
        output.seek(0)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        return send_file(
            io.BytesIO(output.read().encode("utf-8")),
            mimetype="text/csv",
            as_attachment=True,
            download_name=f"duration_report_{ts}.csv",
        )

    # ── Missing / extra report ─────────────────────────────────────────────────
    # Two-section CSV: sheet rows never uploaded, and uploaded files never
    # matched to a sheet row. Same X-Session-Id + fetch()+Blob requirement as
    # every other session-scoped download (see downloadViaFetch in the UI).

    @app.route(f"{API}/report-missing", methods=["GET"])
    @_session
    def download_missing_report(sess: dict) -> Any:
        match_table = sess.get("match_table", [])
        if not match_table:
            return jsonify({"error": "No results yet. Run Analyse first."}), 400
        audio_files = sess.get("audio_files", [])
        unmatched_audio = [f for f in audio_files if not f.get("used")]
        compute_unmatched_status(unmatched_audio, match_table)

        output = io.StringIO()
        _write_missing_sections(output, match_table, unmatched_audio)
        output.seek(0)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        return send_file(
            io.BytesIO(output.read().encode("utf-8")),
            mimetype="text/csv",
            as_attachment=True,
            download_name=f"missing_extra_{ts}.csv",
        )

    # ── Rename formats list ───────────────────────────────────────────────────

    @app.route(f"{API}/rename-formats", methods=["GET"])
    def rename_formats_list() -> Any:
        return jsonify({"formats": list(RENAME_FORMATS.keys())})

    return app


# ── Zip-building helper ──────────────────────────────────────────────────────

def _build_renamed_zip(match_table: list[dict]) -> Optional[str]:
    """Build a temporary zip archive of every currently matched audio file.

    Each entry uses the file's CURRENT on-disk filename, so the archive
    reflects any renaming already applied. Returns the path to the zip
    file, or None if there is nothing to zip.

    Uses ZIP_STORED (no compression) since audio formats are already
    compressed — re-compressing wastes CPU for no size benefit and keeps
    this fast even for large libraries under concurrent load.
    """
    candidates = [
        r for r in match_table
        if r.get("matched_file") and Path(r["matched_file"]).is_file()
    ]
    if not candidates:
        return None

    zip_dir  = tempfile.mkdtemp(prefix="sdm_dl_")
    zip_path = os.path.join(zip_dir, "renamed_audio.zip")
    used_names: set[str] = set()

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_STORED) as zf:
        for row in candidates:
            src = Path(row["matched_file"])
            arcname = src.name
            if arcname in used_names:
                stem, suffix = src.stem, src.suffix
                n = 2
                while f"{stem} ({n}){suffix}" in used_names:
                    n += 1
                arcname = f"{stem} ({n}){suffix}"
            used_names.add(arcname)
            zf.write(src, arcname=arcname)

    return zip_path


# ── Serialisation helpers ──────────────────────────────────────────────────

def _serialise(match_table: list[dict]) -> list[dict]:
    import os
    return [
        {
            "sheet_row":       r.get("sheet_row"),
            "track_title":     r.get("track_title", ""),
            "artist_name":     r.get("artist_name", ""),
            "album":           r.get("album", ""),
            "spotify_link":    r.get("spotify_link", ""),
            "spotify_dur":     r.get("spotify_dur"),
            "spotify_dur_raw": r.get("spotify_dur_raw", ""),
            "matched_file":    r.get("matched_file"),
            "matched_filename":os.path.basename(r["matched_file"]) if r.get("matched_file") else None,
            "file_dur":        r.get("file_dur"),
            "diff":            r.get("diff"),
            "flag_gt1s":       r.get("flag_gt1s", False),
            "match_score":     r.get("match_score", 0),
            "status":          r.get("status", ""),
            "suggested_rename":r.get("suggested_rename", ""),
        }
        for r in match_table
    ]


def _serialise_audio(audio_files: list[dict]) -> list[dict]:
    """Serialise unmatched audio files for the UI."""
    import os
    return [
        {
            "path":     f["path"],
            "filename": os.path.basename(f["path"]),
            "duration": f.get("duration"),
            "status":   f.get("status", "OK"),
            "best_match_score": f.get("best_match_score", 0.0),
            "match_status": f.get("match_status", "NOT_IN_CSV"),
        }
        for f in audio_files
    ]
