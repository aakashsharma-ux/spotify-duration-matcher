"""Duplicate-download detection.

Users often grab the SAME song from more than one downloader site
(SpotiMate, SPOTISAVER, ...), each with its own filename convention.
Because every site's convention is already handled by a pattern in
:mod:`src.filename_parser`, two files that are actually the same track
can normalise to (near-)identical (artist, title) pairs even though
their raw filenames look nothing alike, e.g.::

    SpotiMate.io - Lover - The Troggs.mp3
    The Troggs - Lover (SPOTISAVER).mp3

Both parse to (artist="the troggs", title="lover"). This module finds
duplicate *audio files* directly — independent of whether either file
ends up matched to a sheet row — so the UI/CLI/report can call this
out explicitly instead of silently letting the greedy matcher consume
one copy and leave the other sitting in "unmatched" with no
explanation.

Detection strategy
-------------------
1. Parse every distinct (artist, title) candidate for every file — the
   same candidates :mod:`src.matcher` itself scores against sheet rows.
2. Block file pairs by shared title words (inverted index) so we never
   pay full O(n^2) fuzzy-comparison cost on a large library — only
   files whose titles share at least one significant word are ever
   compared directly.
3. Score each blocked pair with the SAME weighted title/artist formula
   the matcher uses (``matcher.combined_score``), tried across every
   candidate-pairing combination for the two files, keeping the best.
4. Union file pairs whose best score clears ``threshold`` into
   connected components (union-find). ``threshold`` defaults far
   higher than the sheet-matching threshold (90 vs 55) — calling two
   files "duplicates" is a stronger, more consequential claim than
   "this file probably belongs to this sheet row", since a false
   positive here could nudge someone into deleting a file they
   actually needed.

This is a heuristic, not an exhaustive guarantee: if every registered
pattern mis-parses a filename so badly that it shares no title word
with its true duplicate, that pair won't be found. In practice the
existing pattern registry already produces the correct (artist, title)
candidate among its guesses for every convention it supports, so real
duplicates reliably share at least one title word.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .filename_parser import normalise, parse_filename
from .matcher import combined_score

DEFAULT_DUPLICATE_THRESHOLD = 90.0

# Skip single-character "words" when building the title-word blocking
# index — they explode bucket sizes without adding any discriminating
# power (nearly every title contains a stray one-letter token after
# normalisation, e.g. an "a" or "i").
_MIN_TOKEN_LEN = 2

# A word shared by more files than this is almost certainly a generic
# filler word ("remix", "the", "intro"...) rather than a useful
# duplicate signal, and comparing every pair in a bucket that large is
# O(bucket^2) — with real libraries this never triggers (song-title
# vocabulary is heavily long-tailed), but it caps the pathological case
# of a library full of similarly-titled tracks so one huge bucket can
# never turn a routine analysis into a multi-second stall. A file whose
# only shared word is this common one may still be caught via any of
# its OTHER candidate title words.
_MAX_BUCKET_SIZE = 40


@dataclass
class DuplicateGroup:
    """One connected component of audio files believed to be the same song."""

    group_id: int
    files: list[str] = field(default_factory=list)
    best_score: float = 0.0
    # path -> (normalised_title, normalised_artist, score_that_earned_membership)
    file_info: dict[str, tuple[str, str, float]] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "group_id": self.group_id,
            "best_score": round(self.best_score, 1),
            "files": [
                {
                    "path": p,
                    "normalised_title": self.file_info[p][0],
                    "normalised_artist": self.file_info[p][1],
                    "score": round(self.file_info[p][2], 1),
                }
                for p in self.files
            ],
        }


class _UnionFind:
    """Minimal union-find over integer indices 0..n-1."""

    def __init__(self, n: int) -> None:
        self._parent = list(range(n))

    def find(self, x: int) -> int:
        while self._parent[x] != x:
            self._parent[x] = self._parent[self._parent[x]]  # path halving
            x = self._parent[x]
        return x

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self._parent[rb] = ra


def _distinct_candidates(path: str) -> list[tuple[str, str]]:
    """Return the distinct normalised (title, artist) pairs parse_filename() offers for *path*."""
    seen: set[tuple[str, str]] = set()
    out: list[tuple[str, str]] = []
    for _pattern, artist, title in parse_filename(path):
        pair = (normalise(title), normalise(artist))
        if pair not in seen:
            seen.add(pair)
            out.append(pair)
    return out


def find_duplicate_groups(
    audio_files: list[dict],
    threshold: float = DEFAULT_DUPLICATE_THRESHOLD,
) -> list[dict]:
    """Group *audio_files* that appear to be the same song downloaded twice.

    Parameters
    ----------
    audio_files : the dicts produced by ``audio_scanner.scan_audio_files``
        (only the ``"path"`` key is read; duration/status are ignored).
    threshold : minimum weighted title/artist score (0-100) required to
        call two files duplicates. Deliberately stricter than the
        sheet-matching threshold — see module docstring.

    Returns
    -------
    A list of group dicts, each shaped like ``DuplicateGroup.to_dict()``.
    Only groups with 2+ files are returned; an empty list means nothing
    looked duplicated.
    """
    n = len(audio_files)
    if n < 2:
        return []

    paths = [af["path"] for af in audio_files]
    candidates = [_distinct_candidates(p) for p in paths]

    # ── Blocking: only compare files that share a significant title word ──
    word_index: dict[str, list[int]] = {}
    for i, cands in enumerate(candidates):
        words = {
            w
            for title, _artist in cands
            for w in title.split()
            if len(w) >= _MIN_TOKEN_LEN
        }
        for w in words:
            word_index.setdefault(w, []).append(i)

    file_pairs: set[tuple[int, int]] = set()
    for bucket in word_index.values():
        if len(bucket) < 2 or len(bucket) > _MAX_BUCKET_SIZE:
            continue
        distinct = sorted(set(bucket))
        for a_idx in range(len(distinct)):
            for b_idx in range(a_idx + 1, len(distinct)):
                file_pairs.add((distinct[a_idx], distinct[b_idx]))

    # ── Score every candidate-pairing for each blocked file pair ──
    uf = _UnionFind(n)
    # Best (score, title, artist) any single edge has earned a file —
    # used only for the human-readable label in the report.
    best_for_file: dict[int, tuple[float, str, str]] = {}

    for i, j in file_pairs:
        best = 0.0
        best_ti = best_ai = best_tj = best_aj = ""
        for title_i, artist_i in candidates[i]:
            for title_j, artist_j in candidates[j]:
                score = combined_score(title_i, artist_i, title_j, artist_j)
                if score > best:
                    best = score
                    best_ti, best_ai = title_i, artist_i
                    best_tj, best_aj = title_j, artist_j
        if best >= threshold:
            uf.union(i, j)
            if best > best_for_file.get(i, (0.0, "", ""))[0]:
                best_for_file[i] = (best, best_ti, best_ai)
            if best > best_for_file.get(j, (0.0, "", ""))[0]:
                best_for_file[j] = (best, best_tj, best_aj)

    # ── Collect connected components of size >= 2 ──
    components: dict[int, list[int]] = {}
    for i in best_for_file:
        components.setdefault(uf.find(i), []).append(i)

    groups: list[dict] = []
    group_id = 1
    for members in components.values():
        if len(members) < 2:
            continue
        group = DuplicateGroup(group_id=group_id)
        for i in members:
            path = paths[i]
            score, title, artist = best_for_file[i]
            group.files.append(path)
            group.file_info[path] = (title, artist, score)
            group.best_score = max(group.best_score, score)
        groups.append(group.to_dict())
        group_id += 1

    return groups


def remap_duplicate_map(dup_map: dict[str, dict], old_to_new: dict[str, str]) -> dict[str, dict]:
    """Re-key *dup_map* after files on disk have been renamed.

    ``dup_map`` is keyed (and its "siblings" lists are populated) by the
    paths audio files had at scan time. Renaming changes those paths, so
    a lookup by the new path would silently miss — this rewrites both the
    top-level keys and every sibling path using ``old_to_new`` (paths not
    present in the mapping are left as-is, since they weren't renamed).
    """
    def _new(path: str) -> str:
        return old_to_new.get(path, path)

    remapped: dict[str, dict] = {}
    for old_path, info in dup_map.items():
        remapped[_new(old_path)] = {
            **info,
            "siblings": [_new(p) for p in info["siblings"]],
        }
    return remapped


def build_duplicate_map(groups: list[dict]) -> dict[str, dict]:
    """Flatten ``find_duplicate_groups()`` output into a per-path lookup.

    Returns ``{path: {"group_id", "score", "siblings": [other_paths...]}}``
    for every file that belongs to a duplicate group. Paths not involved
    in any duplicate simply aren't keys — callers should use ``.get()``.
    """
    lookup: dict[str, dict] = {}
    for g in groups:
        all_paths = [f["path"] for f in g["files"]]
        for f in g["files"]:
            lookup[f["path"]] = {
                "group_id": g["group_id"],
                "score": f["score"],
                "siblings": [p for p in all_paths if p != f["path"]],
            }
    return lookup
