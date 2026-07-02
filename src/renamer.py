"""Rename matched audio files to reflect their sheet row order.

Default template: ``{idx:03d} - {title} - {artist}{ext}``
e.g.  001 - Stone In My Shoe - Animal Logic.mp3

Returns
-------
(match_table, stats_dict)
  match_table – updated in place; matched_file paths reflect new names
  stats_dict  – {"renamed": int, "skipped_same_name": int,
                  "skipped_not_found": int, "skipped_dest_exists": int,
                  "errors": list[str]}
"""

from __future__ import annotations

import logging
import re
import shutil
from pathlib import Path

logger = logging.getLogger(__name__)

_UNSAFE_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_MULTI_SPACE   = re.compile(r" +")
_MAX_LEN       = 60


def _slugify(text: str, max_len: int = _MAX_LEN) -> str:
    """Return a filesystem-safe version of *text*, preserving spaces."""
    slug = text.strip()
    slug = _UNSAFE_CHARS.sub("", slug)
    slug = _MULTI_SPACE.sub(" ", slug)
    return slug.strip()[:max_len]


def _build_new_name(idx: int, artist: str, title: str, ext: str, template: str) -> str:
    """Apply *template* to produce the new filename (without directory)."""
    try:
        return template.format(
            idx=idx,
            artist=_slugify(artist),
            title=_slugify(title),
            ext=ext,
        )
    except KeyError as exc:
        logger.warning("Bad rename template key %s; falling back.", exc)
        return f"{idx:03d} - {_slugify(title)} - {_slugify(artist)}{ext}"


def rename_matched_files(
    match_table: list[dict],
    template: str = "{idx:03d} - {title} - {artist}{ext}",
    dry_run: bool = False,
) -> tuple[list[dict], dict]:
    """Rename all matched files according to *template*.

    Modifies *match_table* in place (updating ``matched_file`` and
    ``suggested_rename``).  Returns ``(match_table, stats)``.

    stats keys
    ----------
    renamed            – files actually moved/renamed on disk
    skipped_same_name  – files already matching the target name
    skipped_not_found  – source file did not exist on disk
    skipped_dest_exists– destination already exists (collision)
    errors             – list of human-readable error strings
    """
    stats: dict = {
        "renamed":             0,
        "skipped_same_name":   0,
        "skipped_not_found":   0,
        "skipped_dest_exists": 0,
        "errors":              [],
    }
    collision_guard: dict[str, int] = {}

    for row in match_table:
        idx    = row.get("sheet_row", 0)
        artist = row.get("artist_name", "Unknown")
        title  = row.get("track_title", "Unknown")

        # Always set suggested_rename, even for UNMATCHED rows
        row["suggested_rename"] = _build_new_name(idx, artist, title, "", template)

        if not row.get("matched_file"):
            continue

        src = Path(row["matched_file"])
        if not src.exists():
            logger.warning("Source file missing: %s", src)
            stats["skipped_not_found"] += 1
            stats["errors"].append(f"Not found: {src.name}")
            continue

        ext      = src.suffix.lower()
        new_name = _build_new_name(idx, artist, title, ext, template)

        # Deduplicate collisions
        collision_guard[new_name] = collision_guard.get(new_name, 0) + 1
        if collision_guard[new_name] > 1:
            stem   = Path(new_name).stem
            suffix = Path(new_name).suffix
            new_name = f"{stem} (dup{collision_guard[new_name]}){suffix}"

        dest = src.parent / new_name
        row["suggested_rename"] = new_name

        if src == dest:
            logger.debug("Already correctly named: %s", src.name)
            stats["skipped_same_name"] += 1
            continue

        if dest.exists():
            logger.warning("Destination exists, skipping: %s", dest)
            stats["skipped_dest_exists"] += 1
            stats["errors"].append(f"Destination exists: {new_name}")
            continue

        if dry_run:
            logger.info("[DRY RUN] %s → %s", src.name, new_name)
            stats["renamed"] += 1
        else:
            try:
                shutil.move(str(src), str(dest))
                logger.info("Renamed: %s → %s", src.name, new_name)
                row["matched_file"] = str(dest)
                stats["renamed"] += 1
            except OSError as exc:
                logger.error("Failed to rename %s: %s", src.name, exc)
                stats["errors"].append(f"Error renaming {src.name}: {exc}")

    logger.info(
        "Rename done: %d renamed, %d same-name, %d not-found, %d dest-exists, %d errors",
        stats["renamed"], stats["skipped_same_name"],
        stats["skipped_not_found"], stats["skipped_dest_exists"],
        len(stats["errors"]),
    )
    return match_table, stats
