"""Clearing every tag except an operator's exceptions.

Weighted towards what must **not** be removed. This is the most destructive operation in the
application -- an empty keep list is "clear everything" -- so the failure that matters most
is not "a tag survived" but "a tag the operator named as an exception was deleted anyway".

The property test at the bottom states that directly: for any stored list and any keep list,
no value whose normalised form is in the keep list may appear in ``removed``. Every other
test here is an example of the cases the property covers plus the reporting the diff needs.

Genre strings are the live library's (``"Rock, Reggae"``, ``"Reggae; Ska"``,
``"Thrash Metal, Speed Metal, Heavy Metal, Hard Rock"``), because the awkward ones are the
packed values, and an invented fixture would omit them.
"""

from __future__ import annotations

import itertools

import pytest

from metaedit.domain.errors import ValidationError
from metaedit.domain.genre_removal import (
    ClearOutcome,
    is_excepted,
    keep_set,
    normalise,
    plan_clear,
    plan_clears,
    remove_all_except,
    split_kept_component,
)

PACKED_COMMA = "Rock, Reggae"
PACKED_SEMICOLON = "Reggae; Ska"
PACKED_LONG = "Thrash Metal, Speed Metal, Heavy Metal, Hard Rock"


# ------------------------------------------------------------- what must be kept


def test_an_exception_is_never_removed() -> None:
    kept, removed = remove_all_except(["britpop", "Chile", "Rock"], exceptions=["Chile"])
    assert kept == ["Chile"]
    assert removed == ["britpop", "Rock"]


@pytest.mark.parametrize("spelling", ["chile", "CHILE", "Chile", "  chile  ", "ChIlE"])
def test_an_exception_matches_case_insensitively(spelling: str) -> None:
    """The operator types what they see; case must not decide whether it survives."""
    kept, removed = remove_all_except(["britpop", "Chile"], exceptions=[spelling])
    assert kept == ["Chile"]
    assert removed == ["britpop"]


def test_an_exception_list_is_never_substring_matched() -> None:
    """Keeping ``Rock`` must not keep ``Rockabilly``, ``Gothic Rock`` or ``Rock, Reggae``.

    ADR 0015: matching is exact equality after normalisation, never a substring. The
    direction that hurts here is the quiet one -- a keep list that keeps too much makes the
    clear look like it failed, and a keep list that keeps too *little* deletes a genre the
    operator named.
    """
    kept, removed = remove_all_except(
        ["Rock", "Rockabilly", "Gothic Rock", PACKED_COMMA], exceptions=["Rock"]
    )
    assert kept == ["Rock"]
    assert removed == ["Rockabilly", "Gothic Rock", PACKED_COMMA]


def test_an_exception_keeps_a_packed_value_only_when_named_whole() -> None:
    kept, removed = remove_all_except([PACKED_COMMA, "Ska"], exceptions=[PACKED_COMMA])
    assert kept == [PACKED_COMMA]
    assert removed == ["Ska"]


def test_a_packed_value_is_not_kept_by_one_of_its_components() -> None:
    """Without ``decompose`` the packed value is one value, and it is not on the keep list."""
    kept, removed = remove_all_except([PACKED_COMMA, "Ska"], exceptions=["Reggae"])
    assert kept == []
    assert removed == [PACKED_COMMA, "Ska"]


def test_several_exceptions_all_survive() -> None:
    kept, removed = remove_all_except(
        ["britpop", "Chile", "Peru", "Rock"], exceptions=["Chile", "Peru"]
    )
    assert kept == ["Chile", "Peru"]
    assert removed == ["britpop", "Rock"]


def test_every_copy_of_an_exception_survives() -> None:
    """A surviving duplicate is right here: the exception names the value, so all of it stays."""
    kept, removed = remove_all_except(["Chile", "rock", "CHILE"], exceptions=["chile"])
    assert kept == ["Chile", "CHILE"]
    assert removed == ["rock"]


def test_order_of_what_survives_is_preserved() -> None:
    kept, _ = remove_all_except(["Zebra", "Chile", "Apple", "Peru"], exceptions=["Chile", "Peru"])
    assert kept == ["Chile", "Peru"]


def test_an_empty_exception_list_clears_everything() -> None:
    """The intended use, and the reason the diff has to report how much it would empty."""
    kept, removed = remove_all_except(["a", "b", "c"])
    assert kept == []
    assert removed == ["a", "b", "c"]


def test_none_and_empty_values() -> None:
    assert remove_all_except(None) == ([], [])
    assert remove_all_except([]) == ([], [])


# --------------------------------------------------------------- the keep list


def test_a_blank_exception_is_refused() -> None:
    """An empty entry matches nothing, so it would look applied and do nothing."""
    for blank in ("", "   ", "\t"):
        with pytest.raises(ValidationError, match="blank exception"):
            keep_set(["Chile", blank])


def test_keep_set_normalises_and_deduplicates() -> None:
    assert keep_set(["Chile", " chile ", "CHILE"]) == frozenset({"chile"})


def test_keep_set_is_empty_for_an_absent_list() -> None:
    assert keep_set(None) == frozenset()


def test_is_excepted_uses_the_shared_normalisation() -> None:
    keys = keep_set(["R&B"])
    assert is_excepted("r&b", keys)
    assert is_excepted("  R&B  ", keys)
    assert not is_excepted("Rock", keys)
    assert not is_excepted(None, keys)


def test_exceptions_do_not_match_by_case_alone_where_they_differ() -> None:
    keys = keep_set(["Café"])
    assert is_excepted("Café", keys)
    assert is_excepted("CAFÉ", keys)
    # A different value, not a different spelling of the same one.
    assert not is_excepted("Cafe", keys)


# --------------------------------------------------------------- ClearOutcome


def test_plan_clear_reports_what_it_would_do() -> None:
    outcome = plan_clear(["britpop", "Chile"], field="Tags", exceptions=["Chile"])
    assert outcome.field == "Tags"
    assert outcome.before == ["britpop", "Chile"]
    assert outcome.after == ["Chile"]
    assert outcome.removed == ["britpop"]
    assert outcome.changed is True
    assert outcome.emptied is False


def test_plan_clear_flags_an_emptied_field() -> None:
    outcome = plan_clear(["britpop"], field="Tags")
    assert outcome.emptied is True
    assert outcome.after == []


def test_a_field_already_empty_is_not_reported_as_emptied() -> None:
    """Nothing was removed, so nothing was emptied -- the distinction a UI warns on."""
    outcome = plan_clear([], field="Tags")
    assert outcome.changed is False
    assert outcome.emptied is False


def test_a_field_filled_only_with_exceptions_is_not_emptied() -> None:
    outcome = plan_clear(["Chile"], field="Tags", exceptions=["Chile"])
    assert outcome.changed is False
    assert outcome.emptied is False
    assert outcome.after == ["Chile"], "a keep list that keeps everything deletes nothing"


def test_kept_exceptions_reports_keys_this_field_exercised() -> None:
    outcome = plan_clear(
        ["Chile", "Peru", "Rock"], field="Tags", exceptions=["Chile", "Peru", "Ska"]
    )
    assert sorted(outcome.kept_exceptions()) == ["chile", "peru"]


def test_plan_clear_is_json_serialisable() -> None:
    import json

    payload = plan_clear(["Rock"], field="Tags").as_dict()
    assert json.loads(json.dumps(payload))["removed"] == ["Rock"]


def test_clear_outcome_is_frozen() -> None:
    from dataclasses import FrozenInstanceError

    outcome = ClearOutcome(field="Tags", before=["a"], after=[], removed=["a"], exceptions=())
    with pytest.raises(FrozenInstanceError):
        outcome.field = "Genres"  # type: ignore[misc]


# --------------------------------------------------------------- plan_clears


def test_plan_clears_covers_every_requested_field() -> None:
    outcomes = plan_clears(
        {"Tags": ["Chile", "Rock"], "Genres": ["Ska"]},
        fields=("Tags", "Genres"),
        exceptions=["Chile"],
    )
    assert [o.field for o in outcomes] == ["Tags", "Genres"]
    assert outcomes[0].removed == ["Rock"]
    assert outcomes[1].removed == ["Ska"]


def test_plan_clears_reports_an_absent_field_rather_than_omitting_it() -> None:
    outcomes = plan_clears({}, fields=("Tags", "Genres"), exceptions=["Chile"])
    assert [o.field for o in outcomes] == ["Tags", "Genres"]
    assert all(o.before == [] for o in outcomes)
    assert all(not o.changed for o in outcomes)


def test_plan_clears_ignores_a_non_list_field() -> None:
    outcomes = plan_clears({"Tags": "Rock"}, fields=("Tags",))
    assert outcomes[0].changed is False


# ------------------------------------------------------- packed values, decompose


def test_split_kept_component_keeps_only_the_exception() -> None:
    outcome = split_kept_component(PACKED_COMMA, exceptions=["Reggae"])
    assert outcome.kept_components == ["Reggae"]
    assert outcome.removed_components == ["Rock"]
    assert outcome.replacement == "Reggae"
    assert outcome.changed is True


def test_split_kept_component_drops_a_value_with_no_exception_component() -> None:
    outcome = split_kept_component(PACKED_LONG, exceptions=["Reggae"])
    assert outcome.kept_components == []
    assert outcome.replacement is None, "the caller decides that this means 'drop it'"


def test_split_kept_component_leaves_an_untouched_value_alone() -> None:
    outcome = split_kept_component(PACKED_COMMA, exceptions=["Reggae", "Rock"])
    assert outcome.changed is False
    assert outcome.replacement == PACKED_COMMA


def test_split_kept_component_reuses_the_original_separator() -> None:
    """Rewriting ``"Reggae; Ska"`` as ``"Reggae, Ska"`` would be an unrelated restyle."""
    outcome = split_kept_component("Reggae; Ska; Rock", exceptions=["Reggae", "Ska"])
    assert outcome.replacement == "Reggae; Ska"


def test_split_kept_component_is_exact_within_a_component() -> None:
    outcome = split_kept_component("Gothic Rock, Rockabilly", exceptions=["Rock"])
    assert outcome.replacement is None
    assert sorted(outcome.removed_components) == ["Gothic Rock", "Rockabilly"]


# ---------------------------------------------------------------- the property


@pytest.mark.parametrize(
    "values",
    [
        ["Chile", "britpop", "Rock"],
        ["Chile", "CHILE", "chile"],
        [PACKED_COMMA, "Ska", "Reggae"],
        [PACKED_LONG],
        ["", "  ", "Chile"],
        [],
        None,
    ],
)
@pytest.mark.parametrize(
    "exceptions",
    [(), ("Chile",), ("chile", "Reggae"), ("Rock", "Hard Rock"), ("nothing here",)],
)
def test_no_kept_value_is_ever_removed(
    values: list[str] | None, exceptions: tuple[str, ...]
) -> None:
    """The invariant this feature is acceptable only because of.

    Stated over the cross product of awkward inputs rather than one example: for every value
    whose normalisation is in the keep set, it must be in ``kept`` and must not be in
    ``removed``. ``removed`` and ``kept`` are also asserted to partition the input, so a
    value can neither vanish nor be counted twice.
    """
    keys = keep_set(exceptions)
    kept, removed = remove_all_except(values, exceptions=exceptions)

    for value in values or []:
        in_kept = value in kept
        in_removed = value in removed
        if normalise(value) in keys:
            assert in_kept, f"{value!r} is an exception and was removed"
            assert not in_removed
    assert len(kept) + len(removed) == len(values or []), "kept and removed must partition"


def test_the_property_holds_for_every_pair_of_live_values() -> None:
    """A single-exception keep set against the live library's awkward values."""
    live = ["Chile", "Rock", PACKED_COMMA, PACKED_SEMICOLON, "Ska", "Reggae", "R&B/Soul"]
    for keeper, other in itertools.combinations(live, 2):
        kept, removed = remove_all_except([keeper, other], exceptions=[keeper])
        assert kept == [keeper], (keeper, other)
        assert removed == [other], (keeper, other)
