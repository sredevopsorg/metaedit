"""Grouping genres that are the same genre spelled differently.

Weighted towards what must **not** group. A false positive here is not a cosmetic label: the
merge it enables renames values across a library, and the values it would collapse are
genres that share words -- ``Rock`` with ``Rockabilly``, ``Pop`` with ``Pop Rock``. A false
negative is merely unhelpful, so almost every test here asserts a non-merge.

Genre strings are taken from the live library's genre-entity list where possible (``Dark
Wave``/``Darkwave``, ``Goth``/``Gothic``, ``Hip Hop``) and from the way Last.fm tags arrive
(``hip-hop``, ``HipHop``), because the awkward ones are what an invented fixture omits.
"""

from __future__ import annotations

import itertools

import pytest

from metaedit.domain.similarity import (
    SEPARATORS,
    build_preview,
    group_values,
    plan_merge,
    rewrite_groups,
    rewrite_values,
    squash_key,
)

# One genre, seven spellings, including the double space and every joiner the rule treats as
# insignificant and the request named explicitly.
HIP_HOP_SPELLINGS = ["Hip Hop", "hip-hop", "hiphop", "HIP HOP", "hip_hop", "hip.hop", "Hip  Hop"]


# ------------------------------------------------------------------ squash key


@pytest.mark.parametrize("spelling", HIP_HOP_SPELLINGS)
def test_every_hip_hop_spelling_squashes_to_one_key(spelling: str) -> None:
    assert squash_key(spelling) == "hiphop"


@pytest.mark.parametrize("separator", sorted(SEPARATORS))
def test_each_documented_separator_is_ignored(separator: str) -> None:
    """The set of joiners is the rule, so each member is pinned individually."""
    assert squash_key(f"hip{separator}hop") == "hiphop", separator


def test_case_and_unicode_folding_use_the_archive_normalisation() -> None:
    from metaedit.adapters.lastfm.canonical import normalize_tag

    for value in ["Hip Hop", "  Hip   Hop ", "Café Tacvba", "STRASSE", "Straße", "İstanbul"]:
        # The key is the archive's normalised form with separators and spaces removed, so a
        # value carrying neither folds to exactly what the archive would store for it.
        expected = "".join(
            ch for ch in normalize_tag(value) if ch not in SEPARATORS and not ch.isspace()
        )
        assert squash_key(value) == expected


def test_a_non_ascii_case_pair_folds_to_one_key() -> None:
    """``ß``/``ss`` folding is why ``casefold`` is used rather than ``lower``."""
    assert squash_key("STRAßE") == squash_key("strasse")


@pytest.mark.parametrize("value", ["", "   ", None, "-", " - ", "...", "\t"])
def test_a_value_with_no_key_has_no_key(value: str | None) -> None:
    """Blank and separator-only values must not become a group of their own.

    Two values that both squashed to ``""`` would be reported as the same genre, which is
    how a list of junk becomes one "merge".
    """
    assert squash_key(value) == ""


def test_a_single_letter_has_no_key() -> None:
    """Below the floor a key cannot be a genre name, and every such value would group."""
    assert squash_key("X") == ""
    assert squash_key("x") == ""


# --------------------------------------------------------------- must not merge


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("Rock", "Rockabilly"),
        ("Pop", "Pop Rock"),
        ("Rock", "Gothic Rock"),
        ("Hip Hop", "Trip Hop"),
        ("Punk", "Pop Punk"),
        ("Metal", "Heavy Metal"),
        ("Rock", "Post-Rock"),
        ("Soul", "Neo Soul"),
        ("Goth", "Gothic"),
        # The two the live genre-entity list carries as distinct spellings of *one* genre
        # is `Dark Wave`/`Darkwave` -- which the rule merges on purpose. These are the pairs
        # that must not follow it.
        ("Drum & Bass", "Drum and Bass"),
        ("R&B", "Rhythm & Blues"),
        ("Folk", "Folk Rock"),
        ("Jazz", "Jazz Fusion"),
        ("Blues", "Rhythm and Blues"),
    ],
)
def test_word_sharing_genres_never_group(left: str, right: str) -> None:
    """The property the whole rule is acceptable only because of.

    None of these is a spelling of the other: they are genres that share a word. A rule
    that merged on a shared word, or on one key being a prefix of another, would collapse
    half a genre vocabulary into ``rock``.
    """
    assert squash_key(left) != squash_key(right)
    groups = group_values([left, right])
    assert groups == (), f"{left!r} and {right!r} must not be grouped"


def test_a_prefix_is_not_a_spelling_variant() -> None:
    """``Rock`` is a prefix of ``Rockabilly``'s key, and prefix matching is the trap."""
    assert squash_key("rockabilly").startswith(squash_key("rock"))
    assert group_values(["Rock", "Rockabilly", "Gothic Rock"]) == ()


def test_no_pair_of_live_genre_entities_groups_unless_the_keys_match() -> None:
    """Every grouping is exactly ``squash_key`` equality -- never a similarity score.

    The property asserted directly: the partition the function produces is the partition
    the documented rule defines. Anything else (a threshold, a prefix rule, an edit
    distance) would fail this for some pair in this list.
    """
    live = [
        "Alternative Rock",
        "Dark Wave",
        "Darkwave",
        "Goth",
        "Gothic",
        "Hip Hop",
        "Hip-Hop",
        "Electronic",
        "Electronica",
        "Rock",
        "Rockabilly",
        "Pop Rock",
        "Pop",
        "EBM",
        "Synth-pop",
        "Synthpop",
        "Drum & Bass",
        "Drum and Bass",
        "Funk / Soul",
        "Soul",
    ]
    for left, right in itertools.combinations(live, 2):
        grouped = any(left in g.values and right in g.values for g in group_values(live))
        assert grouped == (squash_key(left) == squash_key(right)), (left, right)


# ---------------------------------------------------------------- must merge


def test_hip_hop_spellings_group_together() -> None:
    groups = group_values(HIP_HOP_SPELLINGS)
    assert len(groups) == 1
    # Case-only variants are one *stored* spelling, so the group reports the three distinct
    # spellings rather than seven strings that include two capitalisation duplicates.
    assert set(groups[0].values) == {"Hip Hop", "hip-hop", "hiphop", "hip_hop", "hip.hop"}


def test_dark_wave_and_darkwave_group() -> None:
    """Both are entries in the live genre-entity list, and they are one genre.

    The whitespace squash is what merges them, and that is the documented intent: it is the
    same class of spelling variant as ``Synth-pop``/``Synthpop``.
    """
    groups = group_values(["Dark Wave", "Darkwave"])
    assert len(groups) == 1
    assert set(groups[0].values) == {"Dark Wave", "Darkwave"}


def test_the_same_value_twice_is_not_a_group() -> None:
    """A group of one is not a finding, and a duplicate is one value not two."""
    assert group_values(["Hip Hop"]) == ()
    assert group_values(["Hip Hop", "hip hop", "HIP  HOP"]) == ()


def test_values_with_no_key_are_dropped_rather_than_grouped() -> None:
    assert group_values(["", "   ", "-", "Hip Hop"]) == ()


def test_group_order_is_stable_regardless_of_input_order() -> None:
    """A preview must not depend on the order the vocabulary arrived in."""
    values = ["Zideco Jam", "zideco-jam", "Hip Hop", "hip-hop", "Drum & Bass", "drum&bass"]
    forward = [g.representative for g in group_values(values)]
    backward = [g.representative for g in group_values(list(reversed(values)))]
    assert forward == backward


# ------------------------------------------------------------ representative


def test_the_most_frequent_spelling_represents_its_group() -> None:
    """The form the library already uses is the one to keep: rewriting it gains nothing."""
    group = group_values(["hip-hop", "Hip Hop", "HipHop"], counts={"HipHop": 9})[0]
    assert group.representative == "HipHop"
    assert group.variants[0].count == 9


def test_a_repeated_spelling_is_one_spelling_not_two() -> None:
    """The same value twice is one stored value; capitalisation must not outvote it.

    ``hip-hop`` appearing twice is one spelling with two *occurrences*, so it cannot beat
    ``Hip Hop`` on duplicate count. Weighting a spelling is what ``counts`` is for, and that
    is how a library-sourced vocabulary reports it.
    """
    group = group_values(["hip-hop", "hip-hop", "Hip Hop"])[0]
    assert sorted(group.values) == ["Hip Hop", "hip-hop"]
    assert group.representative == "Hip Hop"


def test_a_library_count_decides_the_representative() -> None:
    group = group_values(["hip-hop", "Hip Hop"], counts={"hip-hop": 40, "Hip Hop": 2})[0]
    assert group.representative == "hip-hop"
    assert group.variants[0].repeated is True


def test_a_tie_prefers_separate_words() -> None:
    """``Hip Hop`` over ``hip-hop``: both equally common, so the conventional form wins."""
    group = group_values(["hip-hop", "Hip Hop"])[0]
    assert group.representative == "Hip Hop"


def test_a_tie_prefers_the_capitalised_spelling() -> None:
    """``Hip Hop`` over ``HipHop``: equally common, and Jellyfin shows the value verbatim."""
    group = group_values(["HipHop", "Hip Hop"])[0]
    assert group.representative == "Hip Hop"


def test_the_representative_is_deterministic() -> None:
    """Two runs over the same vocabulary must choose the same representative.

    Otherwise a preview would not describe what a confirmed apply does.
    """
    values = ["hip.hop", "hip_hop", "hip-hop", "hip hop", "HIPHOP"]
    first = [g.representative for g in group_values(values)]
    for _ in range(5):
        assert [g.representative for g in group_values(list(reversed(values)))] == first


def test_the_representative_is_always_one_of_the_spellings() -> None:
    """A merge rewrites onto a value the library already has; it never invents one."""
    for group in group_values([*HIP_HOP_SPELLINGS, "Dark Wave", "Darkwave"]):
        assert group.representative in group.values


# ------------------------------------------------------------------ proposals


def test_a_group_proposes_every_other_spelling() -> None:
    group = group_values(["hip-hop", "Hip Hop", "HipHop"])[0]
    assert group.proposals == {"HipHop": "Hip Hop", "hip-hop": "Hip Hop"}
    assert group.representative not in group.proposals


def test_a_group_with_one_spelling_needs_no_merge() -> None:
    groups = group_values(HIP_HOP_SPELLINGS)
    assert groups[0].needs_merge is True


# ------------------------------------------------------------ rewrite_values


def test_rewrite_replaces_the_named_spellings_and_keeps_order() -> None:
    after, changed = rewrite_values(
        ["Ska", "hip-hop", "Rock", "HipHop"],
        representative="Hip Hop",
        spellings=["hip-hop", "HipHop", "Hip Hop"],
    )
    # Both are spellings of the value, so both collapse onto the first position: a merge
    # that left two copies would look like it had half worked.
    assert after == ["Ska", "Hip Hop", "Rock"]
    assert changed == [1], "the one position in `after` that changed"

    # An index past the end of `after` would be a crash in a UI that highlighted it.
    assert all(index < len(after) for index in changed)


def test_rewrite_collapses_a_representative_that_is_already_present() -> None:
    """A duplicate left behind looks exactly like the merge having half-failed."""
    after, changed = rewrite_values(
        ["hip-hop", "Hip Hop", "Ska"],
        representative="Hip Hop",
        spellings=["hip-hop", "Hip Hop"],
    )
    assert after == ["Hip Hop", "Ska"]
    assert changed == [0]


def test_rewrite_is_case_insensitive_on_the_spelling_it_matches() -> None:
    """Matching is ``normalize_tag`` equality, the same rule the removal tool uses."""
    after, _ = rewrite_values(["HIP-HOP", "Rock"], representative="Hip Hop", spellings=["hip-hop"])
    assert after == ["Hip Hop", "Rock"]


def test_rewrite_never_touches_a_value_not_in_the_group() -> None:
    """The confirmed group is the whole scope of the write.

    ``Rockabilly`` and ``Trip Hop`` are in the same field and share words with values the
    merge rewrites; a substring or token rewrite would touch them.
    """
    after, changed = rewrite_values(
        ["hip-hop", "Rockabilly", "Trip Hop"],
        representative="Hip Hop",
        spellings=["hip-hop"],
    )
    assert after == ["Hip Hop", "Rockabilly", "Trip Hop"]
    assert changed == [0]


def test_rewrite_of_a_field_with_no_group_member_changes_nothing() -> None:
    assert rewrite_values(["Rock", "Ska"], representative="Hip Hop", spellings=["hip-hop"]) == (
        ["Rock", "Ska"],
        [],
    )


def test_rewrite_handles_none_and_empty() -> None:
    assert rewrite_values(None, representative="Hip Hop", spellings=["hip-hop"]) == ([], [])


def test_a_blank_spelling_in_the_group_matches_nothing() -> None:
    """A blank is not a value, so it must not sweep the empty string out of a list."""
    after, _ = rewrite_values(["", "Rock"], representative="Hip Hop", spellings=[""])
    assert after == ["", "Rock"]


def test_plan_merge_reports_before_and_after() -> None:
    outcome = plan_merge(
        ["hip-hop", "Rock"], representative="Hip Hop", spellings=["hip-hop"], field="Genres"
    )
    assert outcome.field == "Genres"
    assert outcome.to == "Hip Hop"
    assert outcome.before == ["hip-hop", "Rock"]
    assert outcome.after == ["Hip Hop", "Rock"]
    assert outcome.changed == [0]
    assert outcome.changed_any is True


# -------------------------------------------------------------------- preview


def test_preview_lists_groups_and_their_merge_map() -> None:
    result = build_preview(["hip-hop", "Hip Hop", "Rock", "Ska"])
    assert len(result.groups) == 1
    assert result.merge == {"hip-hop": "Hip Hop"}


def test_preview_reports_no_group_when_nothing_is_similar() -> None:
    """The common case, and it must be silent rather than noisy."""
    result = build_preview(["Rock", "Pop", "Jazz", "Blues"])
    assert result.groups == ()
    assert result.merge == {}


def test_preview_truncation_is_reported() -> None:
    result = build_preview(["hip-hop", "Hip Hop"], scanned=42, truncated=True)
    assert result.scanned == 42
    assert result.truncated is True
    assert result.as_dict()["truncated"] is True


def test_preview_keep_refuses_the_whole_group() -> None:
    """Excluding one side of a group must not merge the group anyway."""
    result = build_preview(["Dark Wave", "Darkwave"], keep=["Dark Wave"])
    assert result.groups == ()
    assert len(result.withheld) == 1


def test_preview_an_exception_removes_one_spelling_from_the_group() -> None:
    result = build_preview(["hip-hop", "Hip Hop", "HipHop"], exceptions=["HipHop"])
    assert len(result.groups) == 1
    assert "HipHop" not in result.groups[0].values
    assert result.merge == {"hip-hop": "Hip Hop"}


def test_preview_an_exception_narrowing_a_group_to_one_spelling_withholds_it() -> None:
    result = build_preview(["hip-hop", "Hip Hop"], exceptions=["hip-hop"])
    assert result.groups == ()
    assert len(result.withheld) == 1


def test_preview_reports_an_exception_that_matches_nothing() -> None:
    """The typo case: silence would make a misspelt exclusion look applied."""
    result = build_preview(["hip-hop", "Hip Hop"], exceptions=["hip.hop."])
    hints = " ".join(result.hints)
    assert "hip.hop." in hints
    assert "matches no value" in hints


def test_preview_does_not_report_an_exception_that_matched() -> None:
    result = build_preview(["hip-hop", "Hip Hop"], exceptions=["hip-hop"])
    assert not any("matches no value" in hint for hint in result.hints)


def test_preview_hints_that_a_value_sits_beside_a_word_it_contains() -> None:
    """``Hip Hop`` beside ``Hip`` is not merged -- merging it would be a different genre.

    The hint says so, because a list containing both looks like a merge that worked.
    """
    result = build_preview(["Hip Hop", "Hip"])
    assert result.groups == ()
    hints = [hint.lower() for hint in result.hints]
    assert any("'hip hop'" in hint and "'hip'" in hint for hint in hints), result.hints


def test_a_group_of_one_is_not_a_merge() -> None:
    """The most important "must not": one spelling is not something to collapse."""
    result = build_preview(["hip-hop", "Hip Hop"], exceptions=["hip-hop"])
    assert result.groups == ()
    assert result.merge == {}


def test_preview_is_serialisable() -> None:
    import json

    payload = build_preview(["hip-hop", "Hip Hop"], scanned=3).as_dict()
    round_tripped = json.loads(json.dumps(payload))
    assert round_tripped["merge"] == {"hip-hop": "Hip Hop"}
    assert round_tripped["groups"][0]["merge"] == {"hip-hop": "Hip Hop"}


# ----------------------------------------------------------- rewrite_groups


def test_rewrite_groups_applies_every_group_in_one_pass() -> None:
    after, changed = rewrite_groups(
        ["hip-hop", "drum&bass", "Rock"],
        groups=[
            {"hip-hop": "Hip Hop", "hip hop": "Hip Hop"},
            {"drum&bass": "Drum & Bass"},
        ],
    )
    assert after == ["Hip Hop", "Drum & Bass", "Rock"]
    assert changed == [0, 1]


def test_rewrite_groups_leaves_a_value_outside_every_group_alone() -> None:
    """The property that makes this interchangeable with the single-group rewrite."""
    after, _ = rewrite_groups(["Rockabilly", "Trip Hop"], groups=[{"hip-hop": "Hip Hop"}])
    assert after == ["Rockabilly", "Trip Hop"]


def test_rewrite_groups_collapses_every_spelling_once() -> None:
    after, changed = rewrite_groups(
        ["hip-hop", "HipHop", "Ska"], groups=[{"hip-hop": "Hip Hop", "hiphop": "Hip Hop"}]
    )
    assert after == ["Hip Hop", "Ska"]
    assert changed == [0]


def test_rewrite_groups_indexes_address_the_returned_list() -> None:
    """An index past the end would be a crash in a UI that highlighted it."""
    after, changed = rewrite_groups(
        ["a-hop", "b-hop", "hip-hop", "HipHop"],
        groups=[{"hip-hop": "Hip Hop", "hiphop": "Hip Hop"}],
    )
    assert all(index < len(after) for index in changed)
    assert after[changed[0]] == "Hip Hop"


def test_rewrite_groups_with_none_or_empty_changes_nothing() -> None:
    assert rewrite_groups(None, groups=[{"a": "b"}]) == ([], [])
    assert rewrite_groups(["Rock"], groups=[]) == (["Rock"], [])
