"""Filename parsing for various audio downloader naming conventions.

Architecture
------------
Each naming convention is a dataclass with two methods:
  detect(filename: str) -> bool     — True when this pattern applies
  parse(filename: str) -> (artist_candidate, title_candidate)

``PATTERNS`` is the ordered list the matcher iterates.  Adding a new
convention = adding one dataclass to that list; nothing else changes.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable

# ── Constants ──────────────────────────────────────────────────────────────────

# Parenthetical / bracket junk to strip from filenames before matching
_JUNK_PATTERNS = re.compile(
    r"""
    \(SPOTISAVER\)      |   # explicit SPOTISAVER tag
    \(SP[^)]*\)         |   # truncated SP... tag
    \s*\(feat\.[^)]*\)  |   # feat. tag
    \s*\(ft\.[^)]*\)    |   # ft. tag
    \s*\[feat\.[^\]]*\] |   # [feat.] variant
    \s*\(Radio\s*Edit[^)]*\) |
    \s*\(Remaster[^)]*\)     |
    \s*\(Remix[^)]*\)        |
    \s*\(Live[^)]*\)         |
    \s*\(Official[^)]*\)     |
    \s*\[[^\]]*\]            # any [bracket] tag
    """,
    re.IGNORECASE | re.VERBOSE,
)

# Leading track number: "01. " or "01 - " or "1 - "
_TRACK_NUM_PREFIX = re.compile(r"^\d{1,3}[\.\s\-]+\s*")

# SpotiMate prefix literal (case-insensitive)
_SPOTIMATE_PREFIX = re.compile(r"^spotimate\.io\s*-\s*", re.IGNORECASE)


# ── Normalisation helper ───────────────────────────────────────────────────────

def normalise(text: str) -> str:
    """Unicode-normalise, lowercase, and clean a string for comparison.

    Steps:
      1. NFKD decomposition (handles accents, Cyrillic lookalikes, etc.)
      2. Encode to ASCII, ignoring unconvertible chars (keeps Cyrillic as-is
         after decomposition; they won't convert but we keep the originals for
         the fuzzy matcher which works on Unicode).
      3. Lowercase.
      4. Replace underscores with spaces (SpotiMate uses _ for special chars).
      5. Collapse multiple spaces.
      6. Strip leading/trailing whitespace.
    """
    # NFKD then strip combining chars
    nfkd = unicodedata.normalize("NFKD", text)
    # Keep non-ASCII letters intact so Cyrillic titles still fuzzy-match
    cleaned = "".join(ch for ch in nfkd if not unicodedata.combining(ch))
    cleaned = cleaned.lower()
    cleaned = cleaned.replace("_", " ")
    # Remove characters that are not alphanumeric, space, or hyphen
    cleaned = re.sub(r"[^\w\s\-]", " ", cleaned, flags=re.UNICODE)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def strip_junk(filename_no_ext: str) -> str:
    """Remove downloader tags, feat. annotations, and track-number prefixes."""
    result = _JUNK_PATTERNS.sub("", filename_no_ext)
    result = _TRACK_NUM_PREFIX.sub("", result)
    return result.strip()


def split_on_separator(text: str) -> list[str]:
    """Split on ' - ' (with optional surrounding spaces)."""
    parts = re.split(r"\s+-\s+", text)
    return [p.strip() for p in parts if p.strip()]


# ── Pattern protocol ───────────────────────────────────────────────────────────

@runtime_checkable
class FilenamePattern(Protocol):
    """Protocol that every pattern dataclass must satisfy."""

    name: str

    def detect(self, filename: str) -> bool:
        """Return True when this pattern recognises *filename*."""
        ...

    def parse(self, filename: str) -> tuple[str, str]:
        """Return (artist_candidate, title_candidate) from *filename*.

        Both candidates should already have junk stripped but NOT yet
        normalised — normalisation is applied by the caller.
        """
        ...


# ── Pattern dataclasses ────────────────────────────────────────────────────────

@dataclass
class PatternA:
    """SPOTISAVER / generic Artist - Title (TAG).ext

    Artist comes BEFORE the first ' - '; title comes AFTER.
    Trailing parenthetical tags like '(SPOTISAVER)' are stripped.
    """

    name: str = "Pattern A — SPOTISAVER / Artist - Title"

    def detect(self, filename: str) -> bool:
        """True when not SpotiMate and contains at least one ' - ' separator."""
        if _SPOTIMATE_PREFIX.match(filename):
            return False
        return " - " in filename

    def parse(self, filename: str) -> tuple[str, str]:
        """Split on first ' - '; left = artist, right = title (stripped)."""
        clean = strip_junk(filename)
        parts = split_on_separator(clean)
        if len(parts) < 2:
            return ("", clean)
        artist = parts[0]
        title = " - ".join(parts[1:])  # title may itself contain ' - '
        return (strip_junk(artist), strip_junk(title))


@dataclass
class PatternB:
    """SpotiMate.io — SpotiMate.io - Title - Artist.ext

    Prefix 'SpotiMate.io' identifies this pattern.
    Title is the MIDDLE segment; artist is the LAST segment.
    Underscores treated as spaces.
    """

    name: str = "Pattern B — SpotiMate.io"

    def detect(self, filename: str) -> bool:
        """True when filename starts with 'SpotiMate.io'."""
        return bool(_SPOTIMATE_PREFIX.match(filename))

    def parse(self, filename: str) -> tuple[str, str]:
        """Remove prefix, then split: title = middle, artist = last."""
        without_prefix = _SPOTIMATE_PREFIX.sub("", filename).strip()
        parts = split_on_separator(without_prefix)
        if len(parts) < 2:
            return ("", without_prefix.replace("_", " "))
        artist = parts[-1].replace("_", " ")
        title = " - ".join(parts[:-1]).replace("_", " ")
        return (strip_junk(artist), strip_junk(title))


@dataclass
class PatternC:
    """Generic Title - Artist (no prefix, title comes first)."""

    name: str = "Pattern C — Title - Artist (no prefix)"

    def detect(self, filename: str) -> bool:
        """True when no SpotiMate prefix and contains a separator but would not
        have been detected by Pattern A (Pattern A and C overlap; C is a fallback
        with reversed roles).

        Note: the matcher scores both role assignments and picks the winner,
        so PatternC's contribution is to provide the reversed (title, artist)
        candidate for the scorer to evaluate.
        """
        if _SPOTIMATE_PREFIX.match(filename):
            return False
        return " - " in filename

    def parse(self, filename: str) -> tuple[str, str]:
        """Split on last ' - '; right = artist, left = title."""
        clean = strip_junk(filename)
        parts = split_on_separator(clean)
        if len(parts) < 2:
            return ("", clean)
        artist = parts[-1]
        title = " - ".join(parts[:-1])
        return (strip_junk(artist), strip_junk(title))


@dataclass
class PatternD:
    """Track number prefix: NNN. Artist - Title or NNN - Artist - Title."""

    name: str = "Pattern D — Track-number prefix"

    def detect(self, filename: str) -> bool:
        """True when filename starts with a leading numeric index."""
        return bool(_TRACK_NUM_PREFIX.match(filename))

    def parse(self, filename: str) -> tuple[str, str]:
        """Strip the numeric prefix then delegate to Pattern A logic."""
        without_num = _TRACK_NUM_PREFIX.sub("", filename).strip()
        return PatternA().parse(without_num)


@dataclass
class PatternE:
    """Feat / Remix annotations embedded in otherwise normal filenames."""

    name: str = "Pattern E — Feat/Remix annotation"
    _delegate: PatternA = field(default_factory=PatternA, repr=False)

    def detect(self, filename: str) -> bool:
        """True when filename contains a feat./ft./remix/remaster tag."""
        return bool(re.search(r"\(feat\.\s|\(ft\.\s|\(remix|\(remaster|\(radio edit",
                               filename, re.IGNORECASE))

    def parse(self, filename: str) -> tuple[str, str]:
        """Strip feat/remix tags then parse as Artist - Title."""
        cleaned = strip_junk(filename)
        return self._delegate.parse(cleaned)


# ── Pattern registry ───────────────────────────────────────────────────────────
# Order matters: more-specific patterns should come before generic ones.
# Add new patterns here; the matcher iterates the full list automatically.

PATTERNS: list[FilenamePattern] = [  # type: ignore[type-arg]
    PatternB(),   # SpotiMate — most specific (unique prefix)
    PatternD(),   # Track-number prefix
    PatternE(),   # Feat/remix annotation
    PatternA(),   # Artist - Title (SPOTISAVER-style)
    PatternC(),   # Title - Artist (reversed fallback)
]


# ── Public API ─────────────────────────────────────────────────────────────────

def parse_filename(filepath: str) -> list[tuple[str, str, str]]:
    """Parse a filepath into (pattern_name, artist_candidate, title_candidate).

    Returns all patterns that detect the filename, each producing one candidate
    tuple so the matcher can score all of them and pick the best.
    """
    stem = Path(filepath).stem  # strip extension

    candidates: list[tuple[str, str, str]] = []
    seen_patterns: set[str] = set()

    for pattern in PATTERNS:
        if pattern.detect(stem):
            artist, title = pattern.parse(stem)
            # Avoid returning identical (artist, title) from overlapping patterns
            key = (pattern.name, normalise(artist), normalise(title))
            if key not in seen_patterns:
                seen_patterns.add(key)
                candidates.append((pattern.name, artist, title))

    if not candidates:
        # Last-resort: treat entire stem as title, no artist
        candidates.append(("Pattern fallback", "", stem))

    return candidates
