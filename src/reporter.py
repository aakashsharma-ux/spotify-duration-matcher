"""Report generation: colour-coded Rich terminal table and CSV exports."""

from __future__ import annotations

import csv
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

from rich.console import Console
from rich.table import Table
from rich import box

logger = logging.getLogger(__name__)

# ── Colour scheme ──────────────────────────────────────────────────────────────
COLOR_OK = "green"
COLOR_MISMATCH = "red"
COLOR_UNMATCHED = "yellow"
COLOR_READ_ERROR = "bright_red"
COLOR_INFO_ONLY = "cyan"

# ── CSV column order ───────────────────────────────────────────────────────────
CSV_COLUMNS = [
    "#",
    "track_title",
    "artist_name",
    "album",
    "spotify_link",
    "spotify_dur",
    "matched_file",
    "file_dur",
    "diff_seconds",
    "flag_1s",
    "match_score",
    "status",
    "suggested_rename",
    "duplicate_of",
]


def _row_color(row: dict) -> str:
    """Return the Rich colour string for a given result row."""
    status = row.get("status", "")
    if status == "OK" or status == "INFO_ONLY":
        return COLOR_OK
    if status in ("MISMATCH", "READ_ERROR"):
        return COLOR_MISMATCH
    return COLOR_UNMATCHED  # UNMATCHED or anything else


def _format_dur(dur: Optional[float]) -> str:
    """Format a duration float as M:SS.xx or empty string."""
    if dur is None:
        return ""
    minutes = int(dur // 60)
    seconds = dur % 60
    return f"{minutes}:{seconds:05.2f}"


def _format_diff(diff: Optional[float]) -> str:
    """Format duration difference with a sign prefix."""
    if diff is None:
        return "N/A"
    sign = "+" if diff >= 0 else ""
    return f"{sign}{diff:.2f}s"


def print_rich_table(match_table: list[dict], console: Console) -> None:
    """Render a colour-coded result table to the terminal.

    Green  = diff ≤ tolerance (Match / OK)
    Red    = diff > tolerance (Mismatch) or READ_ERROR
    Yellow = no file found (Unmatched)
    """
    table = Table(
        title="Duration Match Results",
        box=box.ROUNDED,
        show_lines=True,
        header_style="bold white on grey23",
    )

    table.add_column("#", style="dim", width=4, justify="right")
    table.add_column("Track Title", min_width=22)
    table.add_column("Artist", min_width=16)
    table.add_column("Spotify Dur", justify="right", width=11)
    table.add_column("File Dur", justify="right", width=11)
    table.add_column("Diff", justify="right", width=10)
    table.add_column(">1s?", justify="center", width=5)
    table.add_column("Score", justify="right", width=6)
    table.add_column("Status", width=12)
    table.add_column("Matched File", overflow="fold", min_width=20)

    for row in match_table:
        color = _row_color(row)
        flag_cell = "[red]YES[/red]" if row.get("flag_gt1s") else ""
        matched_file = Path(row["matched_file"]).name if row.get("matched_file") else "—"

        table.add_row(
            str(row["sheet_row"]),
            row.get("track_title", ""),
            row.get("artist_name", ""),
            _format_dur(row.get("spotify_dur")),
            _format_dur(row.get("file_dur")),
            _format_diff(row.get("diff")),
            flag_cell,
            str(row.get("match_score", "")),
            row.get("status", ""),
            matched_file,
            style=color,
        )

    console.print(table)
    _print_summary(match_table, console)


def _print_summary(match_table: list[dict], console: Console) -> None:
    """Print a one-line summary bar below the results table."""
    total = len(match_table)
    matched = sum(1 for r in match_table if r["status"] not in ("UNMATCHED",))
    mismatched = sum(1 for r in match_table if r.get("flag_gt1s"))
    unmatched = sum(1 for r in match_table if r["status"] == "UNMATCHED")
    read_errors = sum(1 for r in match_table if r["status"] == "READ_ERROR")

    console.print(
        f"\n[bold]Summary:[/bold]  "
        f"Total: {total}  |  "
        f"[green]Matched: {matched}[/green]  |  "
        f"[red]Mismatched: {mismatched}[/red]  |  "
        f"[yellow]Unmatched: {unmatched}[/yellow]  |  "
        f"[bright_red]Read Errors: {read_errors}[/bright_red]\n"
    )


def _row_to_csv_dict(row: dict, idx: int, dup_map: Optional[dict] = None) -> dict:
    """Convert a match result row to a flat CSV-ready dict.

    ``dup_map`` is the ``{path: {"group_id", "siblings": [...]}}`` lookup from
    ``dedupe.build_duplicate_map()``. When the row's matched file is part of a
    duplicate group, "duplicate_of" lists the sibling filename(s) so a user
    scanning the CSV can see at a glance that another copy of this same song
    was also uploaded (and may be worth deleting). Optional and defaults to
    None so existing callers that don't have dedupe info keep working as-is.
    """
    dup_info = (dup_map or {}).get(row.get("matched_file"))
    duplicate_of = (
        ", ".join(Path(p).name for p in dup_info["siblings"])
        if dup_info else ""
    )
    return {
        "#": idx,
        "track_title": row.get("track_title", ""),
        "artist_name": row.get("artist_name", ""),
        "album": row.get("album", ""),
        "spotify_link": row.get("spotify_link", ""),
        "spotify_dur": row.get("spotify_dur_raw", ""),
        "matched_file": row.get("matched_file", ""),
        "file_dur": row.get("file_dur", ""),
        "diff_seconds": row.get("diff", ""),
        "flag_1s": "YES" if row.get("flag_gt1s") else "",
        "match_score": row.get("match_score", ""),
        "status": row.get("status", ""),
        "suggested_rename": row.get("suggested_rename", ""),
        "duplicate_of": duplicate_of,
    }


def export_csv_report(
    match_table: list[dict],
    output_dir: Path,
    dup_map: Optional[dict] = None,
) -> tuple[Path, Path]:
    """Write full report and mismatches-only CSV; return both paths."""
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    full_path = output_dir / f"duration_report_{timestamp}.csv"
    mismatch_path = output_dir / f"duration_report_{timestamp}_MISMATCHES_ONLY.csv"

    csv_rows = [_row_to_csv_dict(r, i + 1, dup_map) for i, r in enumerate(match_table)]
    mismatch_rows = [r for r in csv_rows if r["flag_1s"] == "YES"]

    _write_csv(full_path, csv_rows)
    _write_csv(mismatch_path, mismatch_rows)

    logger.info("Full report: %s", full_path)
    logger.info("Mismatches:  %s", mismatch_path)
    return full_path, mismatch_path


def _write_csv(path: Path, rows: list[dict]) -> None:
    """Write *rows* to *path* as a UTF-8 CSV."""
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


# ── Missing / extra report ──────────────────────────────────────────────────
# Two-section CSV: sheet rows with no uploaded file ("not uploaded"), and
# uploaded audio files with no sheet row ("not on sheet"). Shared between the
# CLI (writes to disk) and the web server (streams to the browser) so both
# surfaces stay in sync.

NOT_UPLOADED_COLUMNS = [
    "sheet_row", "track_title", "artist_name", "album",
    "spotify_link", "spotify_dur",
]
NOT_ON_SHEET_COLUMNS = [
    "filename", "full_path", "duration_sec", "closest_match_score_pct",
    "possible_duplicate_of",
]


def _write_missing_sections(
    fh,
    match_table: list[dict],
    unmatched_audio: list[dict],
    dup_map: Optional[dict] = None,
) -> None:
    """Write the two-section missing/extra report body to an open file handle.

    Section 1 lists every sheet row with no matched audio file (in the CSV
    / sheet, but never uploaded). Section 2 lists every uploaded audio file
    that wasn't assigned to any sheet row (uploaded, but not on the sheet) —
    this is exactly where a duplicate download ends up once its sibling
    copy has already been matched to the row, so each row also carries a
    "possible_duplicate_of" hint from ``dedupe.build_duplicate_map()`` when
    available. ``dup_map`` is optional and defaults to None so existing
    callers without dedupe info keep working unchanged.
    """
    writer = csv.writer(fh)
    dup_map = dup_map or {}

    not_uploaded = [r for r in match_table if not r.get("matched_file")]
    writer.writerow(["SONGS IN SHEET BUT NOT UPLOADED"])
    writer.writerow(NOT_UPLOADED_COLUMNS)
    for r in not_uploaded:
        writer.writerow([
            r.get("sheet_row", ""),
            r.get("track_title", ""),
            r.get("artist_name", ""),
            r.get("album", ""),
            r.get("spotify_link", ""),
            r.get("spotify_dur_raw", r.get("spotify_dur", "")),
        ])
    if not not_uploaded:
        writer.writerow(["(none — every sheet row has a matched file)"])

    writer.writerow([])
    writer.writerow(["SONGS UPLOADED BUT NOT ON SHEET"])
    writer.writerow(NOT_ON_SHEET_COLUMNS)
    for f in unmatched_audio:
        dup_info = dup_map.get(f.get("path"))
        possible_dupe = (
            ", ".join(Path(p).name for p in dup_info["siblings"])
            if dup_info else ""
        )
        writer.writerow([
            Path(f["path"]).name,
            f.get("path", ""),
            f.get("duration", ""),
            f.get("best_match_score", ""),
            possible_dupe,
        ])
    if not unmatched_audio:
        writer.writerow(["(none — every uploaded file matched a sheet row)"])


def export_missing_report(
    match_table: list[dict],
    unmatched_audio: list[dict],
    output_dir: Path,
    dup_map: Optional[dict] = None,
) -> Path:
    """Write the missing/extra CSV report to *output_dir*; return its path."""
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = output_dir / f"duration_report_{timestamp}_MISSING_EXTRA.csv"

    with path.open("w", newline="", encoding="utf-8") as fh:
        _write_missing_sections(fh, match_table, unmatched_audio, dup_map)

    logger.info("Missing/extra report: %s", path)
    return path


# ── "Updated info csv" ───────────────────────────────────────────────────────
# A drop-in replacement for the originally-uploaded CSV: same banner row, same
# header row, same columns in the same order -- including every column this
# tool has no idea about (popularity, genres, DA workflow fields, whatever a
# given sheet happens to carry) -- with only the duration-comparison columns
# patched in place. Column matching is by HEADER NAME (case-insensitive,
# whitespace-trimmed), not by fixed position, so this keeps working even if a
# particular sheet's column layout doesn't exactly match sheet_loader.py's own
# fixed-index assumptions for the columns it reads (track_title, artist_name,
# album, release_date, spotify_link, spotify_duration only rely on fixed
# positions -- everything else here goes by name).

# header name (case-insensitive) -> match_table field it gets filled from.
# Anything not listed here is copied through completely unchanged, whether or
# not this tool recognises it.
UPDATED_INFO_TARGETS: dict[str, str] = {
    "track duration file": "file_dur",
    "difference": "diff",
    ">1s difference?": "flag_gt1s",
}
# Deliberately NOT in the map above, even when present in a sheet: a column
# tracking whether something was "uploaded successfully" is, as far as this
# tool can tell, about a separate workflow (e.g. delivery to a client/site) --
# not about whether OUR matcher happened to find a local audio file for the
# row. Overwriting it would conflate two different meanings of "uploaded".
# This mirrors sheet_loader.write_back_to_sheet's own never-overwrite column.


def read_raw_csv_template(csv_path: str) -> tuple[list[str], list[str], dict[int, list[str]]]:
    """Read *csv_path* as a template for the Updated info csv export.

    Unlike sheet_loader.load_from_csv (which extracts only the handful of
    fields this tool understands and discards the rest), this keeps every
    column of every row, keyed by physical row number, so the export can
    reproduce the file exactly except for the cells it's meant to correct.

    Returns (banner_row, header_row, {physical_row_num: raw_row}) where
    physical_row_num follows the same 1=banner, 2=header, 3+=data convention
    used everywhere else in this tool. Rows are captured verbatim, including
    ones sheet_loader itself would skip (blank rows, rows with no title or
    artist) -- the export should mirror the original file row-for-row.
    """
    path = Path(csv_path)
    if not path.exists():
        raise FileNotFoundError(f"CSV file not found: {csv_path}")

    banner_row: list[str] = []
    header_row: list[str] = []
    raw_rows: dict[int, list[str]] = {}

    with path.open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.reader(fh)
        for physical_row_num, row in enumerate(reader, start=1):
            if physical_row_num == 1:
                banner_row = row
            elif physical_row_num == 2:
                header_row = row
            else:
                raw_rows[physical_row_num] = row

    return banner_row, header_row, raw_rows


def _find_header_index(header: list[str], name: str) -> Optional[int]:
    """Case-insensitive, trimmed lookup of *name* in *header*; None if absent."""
    target = name.strip().lower()
    for i, h in enumerate(header):
        if h.strip().lower() == target:
            return i
    return None


def _format_mmss(seconds: Optional[float]) -> str:
    """Seconds -> "M:SS", matching the M:SS/MM:SS convention docs already use.

    Minutes are not zero-padded or capped -- a 71-minute file still parses
    fine back through sheet_loader.parse_duration's plain colon-split logic.
    """
    if seconds is None:
        return ""
    total = max(int(round(seconds)), 0)
    minutes, secs = divmod(total, 60)
    return f"{minutes}:{secs:02d}"


def build_updated_info_rows(
    header: list[str],
    raw_rows: dict[int, list[str]],
    match_table: list[dict],
) -> list[list[str]]:
    """Overlay match_table's computed duration/diff/flag onto raw_rows.

    Every row is emitted in original physical-row order. A row with a
    match_table entry (found via its "sheet_row" physical row number) gets
    its recognised columns patched in place by header name; every other
    column in that row -- and every row with NO match_table entry at all
    (blank rows, rows sheet_loader skipped for missing title/artist) -- is
    copied through byte-for-byte unchanged.
    """
    col_index = {
        field: _find_header_index(header, name)
        for name, field in UPDATED_INFO_TARGETS.items()
    }
    by_sheet_row = {r["sheet_row"]: r for r in match_table if r.get("sheet_row") is not None}

    out_rows: list[list[str]] = []
    for physical_row_num in sorted(raw_rows):
        row = list(raw_rows[physical_row_num])  # copy — never mutate the template
        match_row = by_sheet_row.get(physical_row_num)
        if match_row is not None:
            for field, idx in col_index.items():
                if idx is None:
                    continue  # this sheet has no column by that name — nothing to fill
                while len(row) <= idx:
                    row.append("")  # pad short rows so the value lands in the right column
                if field == "file_dur":
                    row[idx] = _format_mmss(match_row.get("file_dur"))
                elif field == "diff":
                    diff = match_row.get("diff")
                    row[idx] = f"{diff:.2f}" if diff is not None else ""
                elif field == "flag_gt1s":
                    row[idx] = "YES" if match_row.get("flag_gt1s") else ""
        out_rows.append(row)
    return out_rows


def export_updated_info_csv(
    csv_path: str,
    match_table: list[dict],
    output_dir: Path,
) -> Path:
    """CLI convenience: read the original CSV as a template, patch it, write it out."""
    banner, header, raw_rows = read_raw_csv_template(csv_path)
    data_rows = build_updated_info_rows(header, raw_rows, match_table)

    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = output_dir / f"updated_info_{timestamp}.csv"

    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        if banner:
            writer.writerow(banner)
        writer.writerow(header)
        writer.writerows(data_rows)

    logger.info("Updated info CSV: %s", path)
    return path
