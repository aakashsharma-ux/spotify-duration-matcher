# spotify-duration-matcher
LIVE LINK - https://spotify-duration-matcher.onrender.com

A CLI + web hybrid tool that matches downloaded audio files to their corresponding rows in a Google Sheet, compares Spotify-reported durations against actual file durations, flags mismatches, and can rename files to mirror sheet order.

---

## Table of Contents

1. [Project Description](#project-description)
2. [Prerequisites](#prerequisites)
3. [Installation](#installation)
4. [Configuration](#configuration)
5. [CLI Flags Reference](#cli-flags-reference)
6. [Usage Examples](#usage-examples)
7. [Web UI Instructions](#web-ui-instructions)
8. [Google Sheets Setup](#google-sheets-setup)
9. [Adding a New Filename Pattern](#adding-a-new-filename-pattern)
10. [Troubleshooting](#troubleshooting)

---

## Project Description

When you download Spotify tracks via third-party tools (SpotiMate, SPOTISAVER, etc.), the resulting audio files land on disk with inconsistent names and in random order. Meanwhile you maintain a Google Sheet that tracks each song with its Spotify-reported duration in column O.

**spotify-duration-matcher** bridges that gap:

| Step | What it does |
|------|-------------|
| 1 | Loads your Google Sheet (via exported CSV or live API) |
| 2 | Scans your audio folder, extracting durations via mutagen |
| 3 | Fuzzy-matches each file to its sheet row by artist + title |
| 4 | Computes the duration difference (file − Spotify) |
| 5 | Flags every track where the difference exceeds 1 second |
| 6 | Optionally renames files to `001_Artist_Title.mp3` (sheet order) |
| 7 | Optionally writes `file_dur / diff / flag` back to columns Q–S |
| 8 | Exports a full CSV report and a mismatches-only CSV |

---

## Prerequisites

- Python 3.11+
- `ffprobe` on your PATH (part of [ffmpeg](https://ffmpeg.org/download.html)) — used as a fallback when mutagen cannot read a file
- A Google Service Account JSON key (only required for `--sheet` / `--write-sheet` modes)

---

## Installation

```bash
# 1. Clone the repo
git clone https://github.com/youruser/spotify-duration-matcher.git
cd spotify-duration-matcher

# 2. Create a virtual environment (recommended)
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. (Optional) Install as a package
pip install -e .
```

---

## Configuration

Copy or edit `config.yaml` in the project root. CLI flags always override these values.

```yaml
sheet_id: ""                  # Google Sheet ID (from its URL)
credentials_file: "creds.json"  # Path to your GCP service account JSON
audio_dir: ""                 # Default audio folder to scan
recursive: true               # Scan subdirectories
match_threshold: 55           # Minimum fuzzy score (0–100) to accept a match
duration_tolerance_sec: 1.0   # Seconds above which col S is flagged "YES"
rename_template: "{idx:03d}_{artist}_{title}{ext}"
output_dir: "./reports"       # Where CSV reports are saved
cache_db: ".duration_cache.db"  # SQLite cache filename inside audio_dir
web_port: 5050                # Port for --web mode
```

---

## CLI Flags Reference

| Flag | Description |
|------|-------------|
| `--csv PATH` | Load sheet from a locally exported CSV file (offline mode) |
| `--sheet SHEET_ID` | Load sheet live via Google Sheets API (requires `credentials_file`) |
| `--audio-dir PATH` | Folder containing downloaded audio files |
| `--no-recursive` | Do not scan subdirectories (default: recursive) |
| `--write-sheet` | Write `file_dur`, `diff`, `flag` back to columns Q–S (requires `--sheet`) |
| `--sort-rename` | Rename matched files to `NNN_Artist_Title.ext` |
| `--interactive` | After auto-matching, prompt user for unmatched / low-confidence tracks |
| `--web` | Launch the local web UI (port from config, default 5050) |
| `--threshold INT` | Override `match_threshold` from config |
| `--output-dir PATH` | Override `output_dir` from config |
| `--config PATH` | Path to an alternate config.yaml |

---

## Usage Examples

```bash
# Offline: CSV export + local folder — just show results
python -m src.cli --csv my_sheet.csv --audio-dir ~/Downloads/Music

# Online: live sheet, write results back to cols Q–S
python -m src.cli --sheet 1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgVE2upms \
                  --audio-dir ~/Music --write-sheet

# Sort/rename files to mirror sheet order
python -m src.cli --csv my_sheet.csv --audio-dir ~/Music --sort-rename

# Interactive reassign for unmatched / low-confidence tracks
python -m src.cli --csv my_sheet.csv --audio-dir ~/Music --interactive

# Launch web UI
python -m src.cli --web --audio-dir ~/Music

# Full run: live sheet, rename, write back, interactive, custom threshold
python -m src.cli --sheet SHEET_ID --audio-dir ~/Music \
                  --sort-rename --write-sheet --interactive --threshold 65
```

---

## Web UI Instructions

```bash
python -m src.cli --web --audio-dir ~/Music
# Open http://localhost:5050 in your browser
```

The single-page app lets you:

1. **Load Sheet** — paste a Sheet ID (requires `creds.json`) or upload a CSV export.
2. **Set Audio Folder** — enter the server-side path to your audio directory.
3. **Run Matching** — click "Analyse" to trigger matching. Results appear in a colour-coded table:
   - 🟢 Green = duration diff ≤ 1 s
   - 🔴 Red = duration diff > 1 s (mismatch)
   - 🟡 Yellow = file not found (unmatched)
4. **Reassign** — click a row's ✏️ button to pick a different file from the top-5 candidates.
5. **Download Report** — downloads `duration_report_<timestamp>.csv`.
6. **Rename Files** — triggers server-side rename to sheet-order filenames.

---

## Google Sheets Setup

1. Go to [console.cloud.google.com](https://console.cloud.google.com).
2. Create a project → enable **Google Sheets API** and **Google Drive API**.
3. Create a **Service Account** → generate a JSON key → save as `creds.json` next to `config.yaml`.
4. **Share** your Google Sheet with the service account email (editor permission for `--write-sheet`).

---

## Adding a New Filename Pattern

All filename patterns live in `src/filename_parser.py` as dataclass instances in the `PATTERNS` list. To add a new one, add a single dataclass — no other code changes required.

```python
@dataclass
class PatternF:
    name: str = "Pattern F — My New Downloader"

    def detect(self, filename: str) -> bool:
        """Return True when this pattern applies."""
        return filename.lower().startswith("mynewdownloader")

    def parse(self, filename: str) -> tuple[str, str]:
        """Return (artist_candidate, title_candidate)."""
        parts = filename.split(" - ", 1)
        return (parts[1], parts[0]) if len(parts) == 2 else ("", filename)

# Register it — that's all:
PATTERNS.append(PatternF())
```

The matcher will automatically try every pattern in order, score the result, and pick the best candidate.

---

## Troubleshooting

### `config.yaml not found — generating default`
The tool created a default `config.yaml` for you. Edit it with your `sheet_id` and `credentials_file` path.

### `ModuleNotFoundError: No module named 'mutagen'`
Run `pip install -r requirements.txt` inside your virtual environment.

### `ffprobe not found`
Install [ffmpeg](https://ffmpeg.org/download.html) and ensure `ffprobe` is on your `PATH`. It is only used as a fallback when mutagen cannot read a file.

### Duration shows `None` for some files
The file may be corrupt or in an unsupported codec. The tool logs these as `READ_ERROR` and skips them. Check the CSV report's `status` column.

### All tracks show `UNMATCHED`
- Your filenames may not contain both artist and title.
- Lower `match_threshold` in `config.yaml` (try 40).
- Use `--interactive` to manually assign files.

### `gspread.exceptions.SpreadsheetNotFound`
- Confirm the Sheet ID is correct (the long string in the sheet's URL).
- Confirm you shared the sheet with your service account email.

### `Permission denied` when renaming
The audio folder may be read-only. Check file system permissions.

### Cyrillic / accented characters not matching
All filenames are Unicode-normalised (NFKD) before comparison. If matching still fails, try `--interactive` to assign manually.
