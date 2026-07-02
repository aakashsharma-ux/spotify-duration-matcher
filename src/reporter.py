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


def _row_to_csv_dict(row: dict, idx: int) -> dict:
    """Convert a match result row to a flat CSV-ready dict."""
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
    }


def export_csv_report(match_table: list[dict], output_dir: Path) -> tuple[Path, Path]:
    """Write full report and mismatches-only CSV; return both paths."""
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    full_path = output_dir / f"duration_report_{timestamp}.csv"
    mismatch_path = output_dir / f"duration_report_{timestamp}_MISMATCHES_ONLY.csv"

    csv_rows = [_row_to_csv_dict(r, i + 1) for i, r in enumerate(match_table)]
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
