"""CLI entry point for spotify-duration-matcher."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

import click
import yaml
from rich.console import Console

from .audio_scanner import scan_audio_files
from .dedupe import find_duplicate_groups, build_duplicate_map, remap_duplicate_map, DEFAULT_DUPLICATE_THRESHOLD
from .matcher import match_tracks, compute_unmatched_status
from .reporter import print_rich_table, export_csv_report, export_missing_report, export_updated_info_csv
from .renamer import rename_matched_files
from .sheet_loader import load_sheet, write_back_to_sheet

console = Console()
DEFAULT_CONFIG_PATH = Path(__file__).parent.parent / "config.yaml"
DEFAULT_WEB_PORT    = 5050
DEFAULT_THRESHOLD   = 55
LOW_CONFIDENCE      = 70
TOP_N_CANDIDATES    = 5


def _load_config(path: Path) -> dict:
    if not path.exists():
        _write_default_config(path)
        console.print(f"[yellow]config.yaml not found — generated default at {path}[/yellow]")
    with path.open() as fh:
        return yaml.safe_load(fh) or {}


def _write_default_config(path: Path) -> None:
    import yaml as _y
    defaults = {
        "sheet_id": "", "credentials_file": "creds.json",
        "audio_dir": "", "recursive": True,
        "match_threshold": DEFAULT_THRESHOLD, "duration_tolerance_sec": 1.0,
        "rename_template": "{idx:03d} - {title} - {artist}{ext}",
        "output_dir": "./reports", "cache_db": ".duration_cache.db",
        "web_port": DEFAULT_WEB_PORT,
        "duplicate_threshold": DEFAULT_DUPLICATE_THRESHOLD,
    }
    with path.open("w") as fh:
        _y.dump(defaults, fh, default_flow_style=False)


def _interactive_reassign(match_table: list[dict], audio_files: list[dict]) -> list[dict]:
    for row in match_table:
        if row["status"] not in ("UNMATCHED",) and (row.get("match_score") or 0) >= LOW_CONFIDENCE:
            continue
        console.print(f"\n[yellow]Track:[/yellow] {row['track_title']} — {row['artist_name']}")
        console.print(f"  Current: {row.get('matched_file') or 'none'}")
        unused = [f for f in audio_files if not f.get("used")][:TOP_N_CANDIDATES]
        for i, cand in enumerate(unused, 1):
            console.print(f"    {i}. {Path(cand['path']).name}")
        console.print("  Pick number (0=skip): ", end="")
        choice = input().strip()
        if not choice.isdigit():
            continue
        n = int(choice)
        if 1 <= n <= len(unused):
            chosen = unused[n - 1]
            chosen["used"] = True
            row.update({"matched_file": chosen["path"], "file_dur": chosen["duration"], "status": "MANUAL"})
    return match_table


@click.command(name="spotify-duration-matcher")
@click.option("--csv", "csv_path", default=None)
@click.option("--sheet", "sheet_id", default=None)
@click.option("--audio-dir", default=None)
@click.option("--no-recursive", is_flag=True, default=False)
@click.option("--write-sheet", is_flag=True, default=False)
@click.option("--sort-rename", is_flag=True, default=False)
@click.option("--interactive", is_flag=True, default=False)
@click.option("--web", is_flag=True, default=False)
@click.option("--threshold", default=None, type=int)
@click.option("--output-dir", default=None)
@click.option("--no-microseconds", is_flag=True, default=False,
              help="Compare only whole seconds (ignore sub-second differences).")
@click.option("--dupe-threshold", default=None, type=float,
              help="Min. weighted title/artist score (0-100) to flag two uploaded "
                   "audio files as the same song downloaded twice. Default 90.")
@click.option("--no-dupe-check", is_flag=True, default=False,
              help="Skip duplicate-download detection entirely.")
@click.option("--config", "config_path", default=str(DEFAULT_CONFIG_PATH))
def main(
    csv_path, sheet_id, audio_dir, no_recursive, write_sheet,
    sort_rename, interactive, web, threshold, output_dir,
    no_microseconds, dupe_threshold, no_dupe_check, config_path,
) -> None:
    """Match downloaded audio files to Google Sheet rows and compare durations."""
    cfg = _load_config(Path(config_path))

    eff_sheet      = sheet_id or cfg.get("sheet_id", "")
    eff_audio      = audio_dir or cfg.get("audio_dir", "")
    eff_threshold  = threshold if threshold is not None else cfg.get("match_threshold", DEFAULT_THRESHOLD)
    eff_output     = Path(output_dir or cfg.get("output_dir", "./reports"))
    eff_recursive  = not no_recursive and cfg.get("recursive", True)
    eff_microsec   = not no_microseconds
    eff_dupe_thresh= dupe_threshold if dupe_threshold is not None else cfg.get("duplicate_threshold", DEFAULT_DUPLICATE_THRESHOLD)
    creds_file     = cfg.get("credentials_file", "creds.json")
    tolerance      = cfg.get("duration_tolerance_sec", 1.0)
    rename_tpl     = cfg.get("rename_template", "{idx:03d} - {title} - {artist}{ext}")
    cache_db       = cfg.get("cache_db", ".duration_cache.db")
    web_port       = cfg.get("web_port", DEFAULT_WEB_PORT)

    if web:
        from .web_server import create_app
        app = create_app(cfg)
        console.print(f"[cyan]Web UI: http://localhost:{web_port}[/cyan]")
        app.run(host="0.0.0.0", port=web_port, debug=False)
        return

    if not csv_path and not eff_sheet:
        console.print("[red]Error:[/red] Provide --csv or --sheet."); sys.exit(1)
    if not eff_audio:
        console.print("[red]Error:[/red] Provide --audio-dir."); sys.exit(1)
    if write_sheet and not eff_sheet:
        console.print("[red]Error:[/red] --write-sheet requires --sheet."); sys.exit(1)

    audio_path = Path(eff_audio)
    if not audio_path.exists():
        console.print(f"[red]Error:[/red] audio-dir does not exist: {audio_path}"); sys.exit(1)

    eff_output.mkdir(parents=True, exist_ok=True)

    console.print("[cyan]Loading sheet…[/cyan]")
    try:
        sheet_rows, gsheet_obj = load_sheet(csv_path=csv_path, sheet_id=eff_sheet or None, creds_file=creds_file)
    except Exception as exc:
        console.print(f"[red]Failed to load sheet:[/red] {exc}"); sys.exit(1)

    if not sheet_rows:
        console.print("[red]Error:[/red] Sheet has no data rows."); sys.exit(1)

    console.print("[cyan]Scanning audio files…[/cyan]")
    audio_files = scan_audio_files(audio_dir=audio_path, recursive=eff_recursive, cache_db_name=cache_db)

    console.print("[cyan]Matching…[/cyan]")
    match_table, unmatched_audio = match_tracks(
        sheet_rows=sheet_rows, audio_files=audio_files,
        threshold=eff_threshold, tolerance=tolerance,
        use_microseconds=eff_microsec,
    )

    if interactive:
        match_table = _interactive_reassign(match_table, audio_files)

    # Re-sync used status and compute unmatched status categorization
    matched_paths = {r["matched_file"] for r in match_table if r.get("matched_file")}
    for af in audio_files:
        af["used"] = af["path"] in matched_paths
    unmatched_audio = [f for f in audio_files if not f.get("used")]
    compute_unmatched_status(unmatched_audio, sheet_rows)

    # Duplicate-download detection: same song downloaded from two sites
    # under two different filename conventions (e.g. SpotiMate.io vs
    # SPOTISAVER) parses to the same (artist, title) and would otherwise
    # just look like an unexplained "unmatched" file — flag it explicitly.
    dup_groups = [] if no_dupe_check else find_duplicate_groups(audio_files, threshold=eff_dupe_thresh)
    dup_map = build_duplicate_map(dup_groups)

    print_rich_table(match_table, console)

    if unmatched_audio:
        console.print(f"\n[yellow]{len(unmatched_audio)} audio file(s) not matched to any sheet row.[/yellow]")
        for af in unmatched_audio:
            dup_info = dup_map.get(af["path"])
            if dup_info:
                siblings = ", ".join(Path(p).name for p in dup_info["siblings"])
                status_str = f"[magenta]Possible duplicate of: {siblings}[/magenta]"
            elif af.get("match_status") == "NOT_IN_CSV":
                status_str = "[red]Not in CSV[/red]"
            else:
                status_str = f"[orange3]No Match Found ({af.get('best_match_score', 0)}%)[/orange3]"
            console.print(f"  • {Path(af['path']).name} — {status_str}")

    if dup_groups:
        console.print(
            f"\n[magenta]⚠ {len(dup_groups)} possible duplicate download(s) detected "
            f"(same song, different filename format):[/magenta]"
        )
        for g in dup_groups:
            console.print(f"  Group (match {g['best_score']}%):")
            for f in g["files"]:
                tag = "[green]kept — matched[/green]" if f["path"] in matched_paths else "[yellow]extra copy[/yellow]"
                console.print(f"    - {Path(f['path']).name}  [{tag}]")

    if sort_rename:
        console.print("[cyan]Renaming files…[/cyan]")
        old_matched_files = {r["sheet_row"]: r.get("matched_file") for r in match_table if r.get("matched_file")}
        match_table, rename_stats = rename_matched_files(match_table, rename_tpl)
        old_to_new = {
            old: new
            for row in match_table
            if (old := old_matched_files.get(row["sheet_row"])) and (new := row.get("matched_file")) and old != new
        }
        dup_map = remap_duplicate_map(dup_map, old_to_new)
        console.print(
            f"[green]Renamed {rename_stats['renamed']} file(s).[/green] "
            f"({rename_stats['skipped_same_name']} already correct, "
            f"{rename_stats['skipped_not_found']} not found, "
            f"{rename_stats['skipped_dest_exists']} collisions)"
        )
        for err in rename_stats["errors"]:
            console.print(f"  [red]•[/red] {err}")

    if write_sheet and gsheet_obj is not None:
        console.print("[cyan]Writing back to sheet…[/cyan]")
        write_back_to_sheet(gsheet_obj, match_table)

    export_csv_report(match_table, eff_output, dup_map)
    missing_path = export_missing_report(match_table, unmatched_audio, eff_output, dup_map)
    console.print(f"[green]Missing/extra report:[/green] {missing_path}")

    # "Updated info csv": a drop-in replacement for the ORIGINAL uploaded
    # CSV -- same banner, same header, same columns in the same order --
    # with only the duration/diff/flag cells corrected. Only makes sense
    # when there was an actual CSV to use as a template; for --sheet mode
    # the equivalent is --write-sheet writing straight back to the live
    # sheet, so there's nothing to export here.
    if csv_path:
        updated_info_path = export_updated_info_csv(csv_path, match_table, eff_output)
        console.print(f"[green]Updated info CSV:[/green] {updated_info_path}")

    console.print("[green]Done.[/green]")


if __name__ == "__main__":
    main()
