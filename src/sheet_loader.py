"""Load track data from a local CSV export or the Google Sheets API.

Column map (0-indexed, col A = 0):
  A=0  track_title       B=1  artist_name      C=2  album
  D=3  release_date      E-H=4-7 reserved      I=8  spotify_link
  J=9  da_name           K=10 date             L=11 status
  M=12 site_us           N=13 num_listens
  O=14 spotify_duration  (READ)
  P=15 reserved
  Q=16 file_duration     (WRITE)
  R=17 difference        (WRITE)
  S=18 flag_gt1s         (WRITE — "YES" or "")
  T=19 uploaded_su       (do NOT overwrite)
"""

from __future__ import annotations

import csv
import logging
import re
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

# ── Column indices ─────────────────────────────────────────────────────────────
COL_TRACK_TITLE = 0
COL_ARTIST_NAME = 1
COL_ALBUM = 2
COL_RELEASE_DATE = 3
COL_SPOTIFY_LINK = 8
COL_SPOTIFY_DUR = 14   # O — read
COL_FILE_DUR = 16      # Q — write
COL_DIFFERENCE = 17    # R — write
COL_FLAG = 18          # S — write

# Google Sheets row where data starts (1-indexed): row 1=banner, 2=header, 3+=data
SHEET_DATA_START_ROW = 3

# Column letters for gspread update range
WRITE_COL_Q = "Q"
WRITE_COL_R = "R"
WRITE_COL_S = "S"


def parse_duration(raw: str | None) -> Optional[float]:
    """Convert a duration string to float seconds.

    Handles:
      M:SS      →  3:42  → 222.0
      MM:SS     →  03:42 → 222.0
      H:MM:SS   →  0:03:42 → 222.0
      plain seconds → "222" → 222.0
      milliseconds  → "222000" → 222.0  (>10 000 is treated as ms)
    """
    if not raw:
        return None

    raw = str(raw).strip()
    if not raw:
        return None

    # Colon-separated formats
    if ":" in raw:
        parts = raw.split(":")
        try:
            parts_float = [float(p) for p in parts]
        except ValueError:
            return None

        if len(parts_float) == 2:
            return parts_float[0] * 60 + parts_float[1]
        if len(parts_float) == 3:
            return parts_float[0] * 3600 + parts_float[1] * 60 + parts_float[2]
        return None

    # Numeric
    try:
        value = float(raw)
    except ValueError:
        return None

    # Treat values > 10 000 as milliseconds
    if value > 10_000:
        return round(value / 1000, 2)
    return round(value, 2)


def _row_to_dict(row: list[str], row_number: int) -> dict:
    """Convert a list of cell values to a normalised track dict."""
    def cell(idx: int) -> str:
        return row[idx].strip() if idx < len(row) else ""

    return {
        "sheet_row": row_number,
        "track_title": cell(COL_TRACK_TITLE),
        "artist_name": cell(COL_ARTIST_NAME),
        "album": cell(COL_ALBUM),
        "release_date": cell(COL_RELEASE_DATE),
        "spotify_link": cell(COL_SPOTIFY_LINK),
        "spotify_dur_raw": cell(COL_SPOTIFY_DUR),
        "spotify_dur": parse_duration(cell(COL_SPOTIFY_DUR)),
    }


def load_from_csv(csv_path: str) -> list[dict]:
    """Read track rows from a locally exported CSV file.

    Skips row 1 (banner) and row 2 (header).
    """
    path = Path(csv_path)
    if not path.exists():
        raise FileNotFoundError(f"CSV file not found: {csv_path}")

    rows: list[dict] = []
    with path.open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.reader(fh)
        for physical_row_num, row in enumerate(reader, start=1):
            if physical_row_num <= 2:  # skip banner + header
                continue
            if not any(cell.strip() for cell in row):
                continue  # skip empty rows
            track = _row_to_dict(row, physical_row_num)
            if not track["track_title"] and not track["artist_name"]:
                continue
            rows.append(track)

    logger.info("Loaded %d tracks from CSV %s", len(rows), csv_path)
    return rows


def load_from_gsheet(sheet_id: str, creds_file: str) -> tuple[list[dict], Any]:
    """Read track rows from a live Google Sheet.

    Returns (rows, worksheet_object) so the caller can write back.
    """
    try:
        import gspread
        from oauth2client.service_account import ServiceAccountCredentials
    except ImportError as exc:
        raise ImportError(
            "gspread / oauth2client not installed. Run: pip install gspread oauth2client"
        ) from exc

    scope = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = ServiceAccountCredentials.from_json_keyfile_name(creds_file, scope)
    client = gspread.authorize(creds)

    spreadsheet = client.open_by_key(sheet_id)
    worksheet = spreadsheet.sheet1

    all_values = worksheet.get_all_values()
    if len(all_values) < 3:
        return [], worksheet

    rows: list[dict] = []
    for physical_row_num, row in enumerate(all_values, start=1):
        if physical_row_num <= 2:
            continue
        if not any(cell.strip() for cell in row):
            continue
        track = _row_to_dict(row, physical_row_num)
        if not track["track_title"] and not track["artist_name"]:
            continue
        rows.append(track)

    logger.info("Loaded %d tracks from Google Sheet %s", len(rows), sheet_id)
    return rows, worksheet


def load_sheet(
    csv_path: Optional[str],
    sheet_id: Optional[str],
    creds_file: str = "creds.json",
) -> tuple[list[dict], Any]:
    """Unified sheet loader; returns (rows, gsheet_worksheet_or_None)."""
    if csv_path:
        rows = load_from_csv(csv_path)
        return rows, None

    if sheet_id:
        return load_from_gsheet(sheet_id, creds_file)

    raise ValueError("Either csv_path or sheet_id must be provided.")


def write_back_to_sheet(worksheet: Any, match_table: list[dict]) -> None:
    """Write file_dur → col Q, difference → col R, flag → col S.

    Uses batch update for efficiency. Never touches col T.
    """
    if worksheet is None:
        logger.warning("No worksheet object — cannot write back.")
        return

    updates: list[dict] = []

    for row in match_table:
        sheet_row = row.get("sheet_row")
        if sheet_row is None:
            continue

        file_dur = row.get("file_dur")
        diff = row.get("diff")
        flag = row.get("flag_gt1s", "")

        # Format values for the sheet
        q_val = f"{file_dur:.2f}" if file_dur is not None else ""
        r_val = f"{diff:.2f}" if diff is not None else "N/A"
        s_val = "YES" if flag else ""

        updates.append({
            "range": f"{WRITE_COL_Q}{sheet_row}:{WRITE_COL_S}{sheet_row}",
            "values": [[q_val, r_val, s_val]],
        })

    if updates:
        worksheet.batch_update(updates, value_input_option="USER_ENTERED")
        logger.info("Wrote %d rows back to sheet.", len(updates))
