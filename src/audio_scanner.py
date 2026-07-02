"""Scan a directory for audio files and extract their durations.

Uses mutagen for metadata, with ffprobe fallback.
Uncached files are processed concurrently via ThreadPoolExecutor for speed.
"""

from __future__ import annotations

import logging
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

AUDIO_EXTENSIONS: frozenset[str] = frozenset(
    {".mp3", ".flac", ".wav", ".aac", ".m4a", ".ogg",
     ".opus", ".wma", ".ape", ".aiff", ".alac"}
)

# Limit concurrent duration-extraction threads (I/O bound; 8 is a safe default)
MAX_WORKERS = min(8, (os.cpu_count() or 2))


def _extract_with_mutagen(path: Path) -> Optional[float]:
    """Return duration in seconds via mutagen, or None on failure."""
    try:
        from mutagen import File as MutagenFile  # type: ignore
    except ImportError:
        return None
    try:
        audio = MutagenFile(path, easy=True)
        if audio is None:
            return None
        info = getattr(audio, "info", None)
        if info is None:
            return None
        dur = getattr(info, "length", None)
        return round(float(dur), 2) if dur is not None else None
    except Exception as exc:  # noqa: BLE001
        logger.debug("mutagen error on %s: %s", path.name, exc)
    return None


def _extract_with_ffprobe(path: Path) -> Optional[float]:
    """Return duration in seconds via ffprobe subprocess fallback."""
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error",
             "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1",
             str(path)],
            capture_output=True, text=True, timeout=15,
        )
        raw = result.stdout.strip()
        if raw and raw != "N/A":
            return round(float(raw), 2)
    except FileNotFoundError:
        logger.debug("ffprobe not found on PATH.")
    except (subprocess.TimeoutExpired, ValueError) as exc:
        logger.debug("ffprobe error on %s: %s", path.name, exc)
    return None


def extract_duration(path: Path) -> Optional[float]:
    """Extract audio duration (float seconds); returns None on total failure."""
    dur = _extract_with_mutagen(path)
    if dur is not None:
        return dur
    return _extract_with_ffprobe(path)


def _is_audio_file(path: Path) -> bool:
    return path.suffix.lower() in AUDIO_EXTENSIONS


def scan_audio_files(
    audio_dir: Path,
    recursive: bool = True,
    cache_db_name: str = ".duration_cache.db",
) -> list[dict]:
    """Scan *audio_dir* for audio files; return list of file dicts.

    Cached files are loaded instantly. Uncached files are extracted
    concurrently (up to MAX_WORKERS threads) then written back to cache.
    Paths are always resolved to absolute form so downstream rename/reassign
    operations remain valid even if the process's working directory changes.
    """
    from .duration_cache import DurationCache

    audio_dir = audio_dir.resolve()
    cache_db_path = audio_dir / cache_db_name
    glob_pattern  = "**/*" if recursive else "*"
    candidates    = [
        p.resolve() for p in audio_dir.glob(glob_pattern)
        if p.is_file() and _is_audio_file(p)
    ]
    logger.info("Found %d audio files in %s", len(candidates), audio_dir)

    results: list[dict] = []

    with DurationCache(cache_db_path) as cache:
        # ── Separate cached vs uncached ──────────────────────────────────
        pre_cached: dict[str, Optional[float]] = {}
        uncached:   list[Path] = []

        for path in candidates:
            if cache.contains(path):
                pre_cached[str(path)] = cache.get(path)
            else:
                uncached.append(path)

        # ── Concurrently extract uncached durations ───────────────────────
        newly_extracted: dict[str, Optional[float]] = {}
        if uncached:
            workers = min(MAX_WORKERS, len(uncached))
            with ThreadPoolExecutor(max_workers=workers) as pool:
                future_map = {pool.submit(extract_duration, p): p for p in uncached}
                for fut in as_completed(future_map):
                    path = future_map[fut]
                    try:
                        dur = fut.result(timeout=30)
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("Duration extraction failed for %s: %s", path.name, exc)
                        dur = None
                    newly_extracted[str(path)] = dur
                    cache.set(path, dur)

        # ── Build result list in original scan order ──────────────────────
        all_durations = {**pre_cached, **newly_extracted}
        for path in candidates:
            duration = all_durations.get(str(path))
            results.append({
                "path":      str(path),
                "filename":  path.name,
                "extension": path.suffix.lower(),
                "duration":  duration,
                "status":    "READ_ERROR" if duration is None else "OK",
                "used":      False,
            })

    logger.info("Duration extraction complete: %d files.", len(results))
    return results
