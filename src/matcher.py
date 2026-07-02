"""Fuzzy matching engine: assign audio files to sheet rows.

Scoring formula:
  combined_score = 0.6 × title_match + 0.4 × artist_match

Score = 0–100. Files below threshold → UNMATCHED.
One-to-one greedy assignment; a file consumed for one track is not reused.

Returns
-------
match_tracks() → (match_table, unmatched_audio_files)
  match_table          – one dict per sheet row, in original sheet order
  unmatched_audio_files – audio files not assigned to any row
"""

from __future__ import annotations

import logging
import math
from typing import Optional

from rapidfuzz import fuzz

from .filename_parser import normalise, parse_filename

logger = logging.getLogger(__name__)

TITLE_WEIGHT   = 0.6
ARTIST_WEIGHT  = 0.4
UNMATCHED_LABEL = "UNMATCHED"


def _combined_score(
    title_candidate: str,
    artist_candidate: str,
    sheet_title: str,
    sheet_artist: str,
) -> float:
    """Return weighted combined fuzzy score (0–100)."""
    t = fuzz.token_sort_ratio(normalise(title_candidate), normalise(sheet_title))
    a = fuzz.token_sort_ratio(normalise(artist_candidate), normalise(sheet_artist))
    return TITLE_WEIGHT * t + ARTIST_WEIGHT * a


def _best_score_for_file(
    audio_file: dict,
    sheet_title: str,
    sheet_artist: str,
) -> tuple[float, str]:
    """Return (best_combined_score, pattern_name) for one audio file."""
    candidates = parse_filename(audio_file["path"])
    best: float = 0.0
    best_pattern = ""
    for pattern_name, artist_cand, title_cand in candidates:
        score = _combined_score(title_cand, artist_cand, sheet_title, sheet_artist)
        if score > best:
            best = score
            best_pattern = pattern_name
    return best, best_pattern


def _compute_diff_and_flag(
    file_dur: Optional[float],
    spotify_dur: Optional[float],
    tolerance: float,
    use_microseconds: bool = True,
) -> tuple[Optional[float], bool]:
    """Return (precise_diff_seconds, is_flagged).

    When use_microseconds=False both durations are floored to whole seconds
    before flagging, so a file reading 5:03.8 and a Spotify entry of 5:03
    produce diff=0.80 but flag=False (floor(303.8)-floor(303.0)=0 ≤ 1).
    The stored diff is always the precise float so toggling the option
    client-side doesn't require a re-analysis.
    """
    if file_dur is None or spotify_dur is None:
        return None, False
    precise_diff = round(file_dur - spotify_dur, 2)
    if use_microseconds:
        compare_diff = precise_diff
    else:
        compare_diff = float(math.floor(file_dur) - math.floor(spotify_dur))
    flagged = abs(compare_diff) > tolerance
    return precise_diff, flagged


def match_tracks(
    sheet_rows: list[dict],
    audio_files: list[dict],
    threshold: int = 55,
    tolerance: float = 1.0,
    use_microseconds: bool = True,
) -> tuple[list[dict], list[dict]]:
    """Match each sheet row to the best available audio file.

    Returns
    -------
    (match_table, unmatched_audio_files)
      match_table          – one result dict per sheet row, sheet order preserved
      unmatched_audio_files – audio files whose ``used`` flag remains False
    """
    available: list[dict] = [dict(f, used=False) for f in audio_files]
    results: list[dict] = []

    for track in sheet_rows:
        sheet_title  = track.get("track_title", "")
        sheet_artist = track.get("artist_name", "")
        spotify_dur  = track.get("spotify_dur")

        # Score every unused file
        scored: list[tuple[float, int, str]] = []
        for idx, af in enumerate(available):
            if af["used"]:
                continue
            score, pattern = _best_score_for_file(af, sheet_title, sheet_artist)
            scored.append((score, idx, pattern))

        scored.sort(key=lambda x: x[0], reverse=True)

        if scored and scored[0][0] >= threshold:
            best_score, best_idx, best_pattern = scored[0]

            # Collision warning
            if len(scored) > 1 and scored[1][0] >= threshold:
                logger.warning(
                    "Score collision on '%s': %.1f vs %.1f",
                    sheet_title, scored[0][0], scored[1][0],
                )

            af = available[best_idx]
            af["used"] = True
            file_dur   = af["duration"]
            diff, flagged = _compute_diff_and_flag(
                file_dur, spotify_dur, tolerance, use_microseconds
            )

            if af["status"] == "READ_ERROR":
                row_status = "READ_ERROR"
            elif spotify_dur is None:
                row_status = "INFO_ONLY"
            elif flagged:
                row_status = "MISMATCH"
            else:
                row_status = "OK"

            results.append({
                "sheet_row":      track["sheet_row"],
                "track_title":    sheet_title,
                "artist_name":    sheet_artist,
                "album":          track.get("album", ""),
                "spotify_link":   track.get("spotify_link", ""),
                "spotify_dur":    spotify_dur,
                "spotify_dur_raw":track.get("spotify_dur_raw", ""),
                "matched_file":   af["path"],
                "file_dur":       file_dur,
                "diff":           diff,
                "flag_gt1s":      flagged,
                "match_score":    round(best_score, 1),
                "match_pattern":  best_pattern,
                "status":         row_status,
                "suggested_rename": "",
            })
        else:
            results.append({
                "sheet_row":      track["sheet_row"],
                "track_title":    sheet_title,
                "artist_name":    sheet_artist,
                "album":          track.get("album", ""),
                "spotify_link":   track.get("spotify_link", ""),
                "spotify_dur":    spotify_dur,
                "spotify_dur_raw":track.get("spotify_dur_raw", ""),
                "matched_file":   None,
                "file_dur":       None,
                "diff":           None,
                "flag_gt1s":      False,
                "match_score":    round(scored[0][0], 1) if scored else 0.0,
                "match_pattern":  "",
                "status":         UNMATCHED_LABEL,
                "suggested_rename": "",
            })

    unmatched_audio = [f for f in available if not f["used"]]
    matched    = sum(1 for r in results if r["status"] != UNMATCHED_LABEL)
    mismatched = sum(1 for r in results if r["flag_gt1s"])
    logger.info(
        "Match: %d/%d matched, %d mismatched, %d unmatched rows, %d unused audio files",
        matched, len(results), mismatched,
        len(results) - matched, len(unmatched_audio),
    )
    return results, unmatched_audio
