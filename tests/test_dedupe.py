"""Unit tests for dedupe.py — duplicate-download detection."""

import pytest

from src.dedupe import find_duplicate_groups, build_duplicate_map, DEFAULT_DUPLICATE_THRESHOLD


def make_audio(path):
    """Minimal audio-file dict shape — dedupe only reads 'path'."""
    return {"path": path}


class TestFindDuplicateGroups:
    def test_empty_input(self):
        assert find_duplicate_groups([]) == []

    def test_single_file_no_groups(self):
        assert find_duplicate_groups([make_audio("/m/a.mp3")]) == []

    def test_no_duplicates_among_distinct_songs(self):
        files = [
            make_audio("/m/The Beatles - Let It Be.mp3"),
            make_audio("/m/Queen - Bohemian Rhapsody.mp3"),
            make_audio("/m/SpotiMate.io - Yesterday - The Beatles.mp3"),
        ]
        assert find_duplicate_groups(files) == []

    def test_classic_spotimate_vs_spotisaver_dupe(self):
        """The exact real-world case: same song, two sites, two conventions."""
        files = [
            make_audio("/m/SpotiMate.io - Lover - The Troggs.mp3"),
            make_audio("/m/The Troggs - Lover (SPOTISAVER).mp3"),
        ]
        groups = find_duplicate_groups(files)
        assert len(groups) == 1
        paths = {f["path"] for f in groups[0]["files"]}
        assert paths == {p["path"] for p in files}
        assert groups[0]["best_score"] >= DEFAULT_DUPLICATE_THRESHOLD

    def test_same_title_different_artist_not_flagged(self):
        """Same title word, but a genuinely different song — must NOT group."""
        files = [
            make_audio("/m/SpotiMate.io - Lover - The Troggs.mp3"),
            make_audio("/m/Taylor Swift - Lover.mp3"),
        ]
        assert find_duplicate_groups(files) == []

    def test_same_artist_different_title_not_flagged(self):
        """Same artist, different song — must NOT group."""
        files = [
            make_audio("/m/SpotiMate.io - Lover - The Troggs.mp3"),
            make_audio("/m/SpotiMate.io - Wild Thing - The Troggs.mp3"),
        ]
        assert find_duplicate_groups(files) == []

    def test_three_way_duplicate_group(self):
        """Same song from three different naming conventions -> one group of 3."""
        files = [
            make_audio("/m/SpotiMate.io - Lover - The Troggs.mp3"),
            make_audio("/m/The Troggs - Lover (SPOTISAVER).mp3"),
            make_audio("/m/003 - The Troggs - Lover.mp3"),
        ]
        groups = find_duplicate_groups(files)
        assert len(groups) == 1
        assert len(groups[0]["files"]) == 3

    def test_apostrophe_and_track_number_noise_still_matches(self):
        files = [
            make_audio("/m/SpotiMate.io - Don't Stop Believin' - Journey.mp3"),
            make_audio("/m/Journey - Dont Stop Believin (SPOTISAVER).mp3"),
            make_audio("/m/001. Journey - Dont Stop Believin.mp3"),
        ]
        groups = find_duplicate_groups(files)
        assert len(groups) == 1
        assert len(groups[0]["files"]) == 3

    def test_feat_tag_stripped_still_matches(self):
        files = [
            make_audio("/m/SpotiMate.io - Shape of You (feat. Nobody) - Ed Sheeran.mp3"),
            make_audio("/m/Ed Sheeran - Shape of You (SPOTISAVER).mp3"),
        ]
        groups = find_duplicate_groups(files)
        assert len(groups) == 1

    def test_two_independent_duplicate_pairs(self):
        """Two unrelated duplicate pairs in the same library -> two separate groups."""
        files = [
            make_audio("/m/SpotiMate.io - Lover - The Troggs.mp3"),
            make_audio("/m/The Troggs - Lover (SPOTISAVER).mp3"),
            make_audio("/m/SpotiMate.io - Yesterday - The Beatles.mp3"),
            make_audio("/m/The Beatles - Yesterday (SPOTISAVER).mp3"),
        ]
        groups = find_duplicate_groups(files)
        assert len(groups) == 2
        sizes = sorted(len(g["files"]) for g in groups)
        assert sizes == [2, 2]

    def test_higher_threshold_excludes_near_matches(self):
        files = [
            make_audio("/m/SpotiMate.io - Lover - The Troggs.mp3"),
            make_audio("/m/The Troggs - Lover (SPOTISAVER).mp3"),
        ]
        assert find_duplicate_groups(files, threshold=101.0) == []

    def test_lower_threshold_more_permissive(self):
        files = [
            make_audio("/m/Artist One - Some Song.mp3"),
            make_audio("/m/Artist Two - Some Sung.mp3"),
        ]
        # Loose enough threshold should catch near-miss spelling; strict shouldn't.
        loose = find_duplicate_groups(files, threshold=40.0)
        strict = find_duplicate_groups(files, threshold=99.0)
        assert len(loose) >= len(strict)

    def test_group_ids_are_unique_and_sequential(self):
        files = [
            make_audio("/m/SpotiMate.io - Lover - The Troggs.mp3"),
            make_audio("/m/The Troggs - Lover (SPOTISAVER).mp3"),
            make_audio("/m/SpotiMate.io - Yesterday - The Beatles.mp3"),
            make_audio("/m/The Beatles - Yesterday (SPOTISAVER).mp3"),
        ]
        groups = find_duplicate_groups(files)
        ids = [g["group_id"] for g in groups]
        assert ids == sorted(set(ids))

    def test_large_shared_word_bucket_does_not_hang(self):
        """Many files sharing one generic word must stay fast (bucket cap)."""
        import time
        files = [make_audio(f"/m/Artist {i} - Remix Track {i}.mp3") for i in range(200)]
        start = time.time()
        find_duplicate_groups(files)
        assert time.time() - start < 5.0


class TestBuildDuplicateMap:
    def test_empty_groups(self):
        assert build_duplicate_map([]) == {}

    def test_maps_every_file_to_its_siblings(self):
        files = [
            make_audio("/m/SpotiMate.io - Lover - The Troggs.mp3"),
            make_audio("/m/The Troggs - Lover (SPOTISAVER).mp3"),
        ]
        groups = find_duplicate_groups(files)
        dup_map = build_duplicate_map(groups)
        assert set(dup_map.keys()) == {p["path"] for p in files}
        a, b = [p["path"] for p in files]
        assert dup_map[a]["siblings"] == [b]
        assert dup_map[b]["siblings"] == [a]
        assert dup_map[a]["group_id"] == dup_map[b]["group_id"]

    def test_file_not_in_any_group_is_absent(self):
        files = [
            make_audio("/m/SpotiMate.io - Lover - The Troggs.mp3"),
            make_audio("/m/The Troggs - Lover (SPOTISAVER).mp3"),
            make_audio("/m/Unrelated - Song.mp3"),
        ]
        groups = find_duplicate_groups(files)
        dup_map = build_duplicate_map(groups)
        assert "/m/Unrelated - Song.mp3" not in dup_map
