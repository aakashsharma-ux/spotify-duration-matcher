# Google Sheet Column Map

This document describes the exact layout of the Google Sheet used by **spotify-duration-matcher**.

---

## Row Structure

| Physical Row | Purpose |
|---|---|
| Row 1 | Merged banner / title row — **skipped by the tool** |
| Row 2 | Header labels — **skipped by the tool** |
| Row 3+ | Data rows (one per track) |

---

## Column Reference

| Col | Letter | Index (0-based) | Field Name | Direction | Notes |
|-----|--------|-----------------|------------|-----------|-------|
| A | A | 0 | `track_title` | READ | Primary track name used for fuzzy matching |
| B | B | 1 | `artist_name` | READ | Primary artist name used for fuzzy matching |
| C | C | 2 | `album` | READ | Album title (informational only) |
| D | D | 3 | `release_date` | READ | Release date (informational only) |
| E | E | 4 | *(reserved)* | SKIP | May be empty — tool skips gracefully |
| F | F | 5 | *(reserved)* | SKIP | May be empty — tool skips gracefully |
| G | G | 6 | *(reserved)* | SKIP | May be empty — tool skips gracefully |
| H | H | 7 | *(reserved)* | SKIP | May be empty — tool skips gracefully |
| I | I | 8 | `spotify_link` | READ | Full Spotify URL for the track |
| J | J | 9 | `da_name` | READ | DA Workflow section — "DA NAME" column |
| K | K | 10 | `date` | READ | Date field in the DA Workflow section |
| L | L | 11 | `status` | READ | Status field in the DA Workflow section |
| M | M | 12 | `site_us` | READ | "Site us" column |
| N | N | 13 | `num_listens` | READ | Number of listens counter |
| **O** | **O** | **14** | **`spotify_duration`** | **READ** | **Spotify-reported duration — PRIMARY comparison column** |
| P | P | 15 | *(reserved/empty)* | SKIP | May be empty |
| **Q** | **Q** | **16** | **`file_duration`** | **WRITE** | **Actual audio file duration extracted by the tool** |
| **R** | **R** | **17** | **`difference`** | **WRITE** | **`file_dur − spotify_dur` in seconds (signed float)** |
| **S** | **S** | **18** | **`flag_gt1s`** | **WRITE** | **`"YES"` if `|diff| > tolerance`, else `""`** |
| T | T | 19 | `uploaded_su` | **DO NOT TOUCH** | Status column managed separately — tool never overwrites |

---

## Read Columns

The tool reads the following columns to build its match table:

- **A** (`track_title`) — matched against audio filenames via fuzzy search
- **B** (`artist_name`) — matched against audio filenames via fuzzy search
- **C** (`album`) — included in CSV export, not used for matching
- **I** (`spotify_link`) — included in CSV export
- **O** (`spotify_duration`) — the Spotify-reported duration; compared against actual file duration

Column O may contain duration in any of five formats:

| Format | Example | Parsed as |
|--------|---------|-----------|
| `M:SS` | `3:42` | 222.0 s |
| `MM:SS` | `03:42` | 222.0 s |
| `H:MM:SS` | `0:03:42` | 222.0 s |
| Plain seconds | `222` | 222.0 s |
| Milliseconds | `222000` | 222.0 s (values > 10 000 are treated as ms) |

---

## Write Columns

When `--write-sheet` is used, the tool writes back to three columns using the Google Sheets API batch update. **Column T is never touched.**

| Column | Value written | Example |
|--------|--------------|---------|
| Q (`file_duration`) | Duration extracted from the audio file, as a decimal string | `"222.54"` |
| R (`difference`) | `file_dur − spotify_dur`, signed, 2 decimal places | `"-1.46"`, `"0.54"` |
| S (`flag_gt1s`) | `"YES"` if `|diff| > duration_tolerance_sec`, else `""` | `"YES"` or `""` |

The tolerance threshold is set via `duration_tolerance_sec` in `config.yaml` (default: `1.0` second).

---

## How the Tool Accesses the Sheet

### CSV Mode (`--csv`)

```
python -m src.cli --csv my_sheet_export.csv --audio-dir ~/Music
```

The tool reads a locally exported CSV. Column indices are 0-based (A=0, B=1, …). The tool skips the first two rows (banner + header) automatically.

Write-back is **not available** in CSV mode. Export to CSV instead.

### Google Sheets API Mode (`--sheet`)

```
python -m src.cli --sheet SHEET_ID --audio-dir ~/Music --write-sheet
```

Requires a GCP Service Account key file (`creds.json`). The sheet must be shared with the service account email (Editor permission for write-back, Viewer for read-only).

The API path accesses `spreadsheet.sheet1` (the first tab). If your data is on a different tab, update `web_server.py` and `sheet_loader.py` to use `worksheet_by_title("YourTabName")`.

---

## Preserving Column T

Column T (`uploaded_su`) is a manually maintained status column. The tool's batch update range is always `Q{row}:S{row}` — it never touches T or any column beyond S.

---

## Adding More Read Columns

To surface additional columns in the match table or CSV export:

1. Add a new constant to `src/sheet_loader.py`:
   ```python
   COL_MY_NEW_FIELD = 20  # column U
   ```
2. Include it in `_row_to_dict()`:
   ```python
   "my_new_field": cell(COL_MY_NEW_FIELD),
   ```
3. Add `"my_new_field"` to `CSV_COLUMNS` in `src/reporter.py`.
4. Include it in the `_row_to_csv_dict()` function.
