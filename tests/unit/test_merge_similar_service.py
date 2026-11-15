"""The service layer of the two operator batches, without a database or a Jellyfin.

What is tested here is the part that decides *what would be written* -- the confirmation
rules that keep a merge from becoming a general-purpose rename, the per-item planning, and
the reports a clear run has to produce before anyone confirms it. The write path itself
(``apply_job``, snapshots, reverts) is covered by the integration tests, against a real
database, because that is where those guarantees actually live.

Weighted towards refusals. Both features exist because an operator asked for something
destructive, so the interesting behaviour is what they must not do: merge a pair the rule
would not have grouped, merge with nothing confirmed, or report success while an exception
matched nothing.
"""

from __future__ import annotations

import pytest

from metaedit.domain.snapshot import NormalizedItem
from metaedit.service import merge_similar

# A normalised artist as `domain.snapshot.from_dto` would build one, with only the fields
# these tests read. `fields` is the whitelist snapshot, so a merge plan sees exactly what
# the write path will carry through unchanged.
BASE_FIELDS: dict[str, object] = {
    "Name": "Alpha",
    "Genres": ["hip-hop", "Rock"],
    "Tags": ["britpop"],
    "Overview": "",
    "ProviderIds": {},
    "LockedFields": [],
}


def _item(**overrides: object) -> NormalizedItem:
    fields = {**BASE_FIELDS, **overrides}
    return NormalizedItem(
        kind="MusicArtist",
        item_id="id-alpha",
        name="Alpha",
        etag="etag-1",
        fields=fields,
    )


def _rule(
    representative: str = "Hip Hop", spellings: tuple[str, ...] = ("hip-hop", "Hip Hop")
) -> merge_similar.MergeRule:
    return merge_similar.MergeRule(
        field="Genres", representative=representative, spellings=spellings
    )


# ------------------------------------------------------------------ confirming


def test_a_confirmed_group_becomes_a_rule() -> None:
    rules = merge_similar.build_rules(
        [{"field": "Genres", "representative": "Hip Hop", "spellings": ["hip-hop", "Hip Hop"]}]
    )
    assert rules == (_rule(),)
    assert rules[0].squashed == "hiphop"


def test_no_confirmed_groups_is_refused() -> None:
    """A merge never runs automatically: an empty confirmation is a request to merge all.

    Accepting an empty list would make the endpoint merge whatever it found, which is the
    exact behaviour this feature was specified not to have.
    """
    with pytest.raises(Exception, match="never runs automatically"):
        merge_similar.build_rules([])


def test_a_group_that_is_not_a_spelling_pair_is_refused() -> None:
    """The endpoint must not become a general-purpose rename.

    ``Rock`` -> ``Rockabilly`` is a group the similarity rule would never produce, so
    confirmation cannot invent it.
    """
    with pytest.raises(Exception, match="not the same genre spelled differently"):
        merge_similar.build_rules(
            [
                {
                    "field": "Genres",
                    "representative": "Rock",
                    "spellings": ["Rock", "Rockabilly"],
                }
            ]
        )


def test_a_group_with_a_representative_outside_it_is_refused() -> None:
    """A merge rewrites onto a value the library has; it does not invent a new one."""
    with pytest.raises(Exception, match="does not contain its own representative"):
        merge_similar.build_rules(
            [
                {
                    "field": "Genres",
                    "representative": "Hip-Hop",
                    "spellings": ["hip hop", "HipHop"],
                }
            ]
        )


def test_a_group_of_one_spelling_is_refused() -> None:
    with pytest.raises(Exception, match="at least two spellings"):
        merge_similar.build_rules(
            [{"field": "Genres", "representative": "Hip Hop", "spellings": ["Hip Hop"]}]
        )


def test_a_case_duplicate_is_not_two_spellings() -> None:
    with pytest.raises(Exception, match="one spelling repeated"):
        merge_similar.build_rules(
            [{"field": "Genres", "representative": "Hip Hop", "spellings": ["Hip Hop", "hip hop"]}]
        )


def test_an_unknown_field_is_refused() -> None:
    """A closed field set: this endpoint writes to Jellyfin, and ``Overview`` is not a genre."""
    with pytest.raises(Exception, match="cannot merge values in 'Overview'"):
        merge_similar.build_rules(
            [{"field": "Overview", "representative": "Hip Hop", "spellings": ["hip-hop"]}]
        )


# --------------------------------------------------------------- per-item plan


def test_plan_for_item_rewrites_only_the_group() -> None:
    plan, rewritten = merge_similar.plan_for_item(_item(), rules=[_rule()])

    assert rewritten == {"Genres": [0]}
    [change] = plan.proposed
    assert change.field == "Genres"
    assert change.current == ["hip-hop", "Rock"]
    assert change.proposed == ["Hip Hop", "Rock"]
    assert change.selected is True, "the operator named this group, so it is pre-selected"


def test_plan_for_item_is_applicable_without_further_review() -> None:
    """The default selection is the merge itself, whose job is confirmed by a job id.

    Same reasoning as the removal feature's operator confidence: nothing was *matched*, so
    there is no match quality to review. The review happened at the preview/confirmation.
    """
    plan, _ = merge_similar.plan_for_item(_item(), rules=[_rule()])
    assert plan.default_selection == ["Genres"]


def test_plan_for_item_leaves_a_value_not_in_the_group_alone() -> None:
    """``Rockabilly`` shares no key with the group, so a merge must not touch it."""
    plan, rewritten = merge_similar.plan_for_item(
        _item(Genres=["hip-hop", "Rockabilly"]), rules=[_rule()]
    )
    [change] = plan.proposed
    assert change.proposed == ["Hip Hop", "Rockabilly"]
    assert rewritten == {"Genres": [0]}


def test_plan_for_item_proposes_nothing_when_the_item_is_already_canonical() -> None:
    plan, rewritten = merge_similar.plan_for_item(
        _item(Genres=["Hip Hop", "Rock"]), rules=[_rule()]
    )
    assert plan.proposed == []
    assert plan.default_selection == []
    assert rewritten == {}


def test_plan_for_item_merges_two_groups_on_one_field_in_one_pass() -> None:
    """Both groups live in one array, so the field is rewritten once, not twice.

    Applying them sequentially would let the second pass see the first's output -- and a
    representative that happened to be another group's spelling would be rewritten twice.
    """
    rules = (
        _rule("Hip Hop", ("hip-hop", "Hip Hop")),
        merge_similar.MergeRule(
            field="Genres", representative="Drum & Bass", spellings=("drum&bass", "Drum & Bass")
        ),
    )
    plan, rewritten = merge_similar.plan_for_item(
        _item(Genres=["hip-hop", "drum&bass", "Rock"]), rules=rules
    )
    # One field is one change, however many groups touch it.
    [change] = plan.proposed
    assert change.proposed == ["Hip Hop", "Drum & Bass", "Rock"]
    assert rewritten == {"Genres": [0, 1]}


def test_a_locked_field_is_refused_by_the_shared_selection_rule() -> None:
    """Jellyfin's ``LockedFields`` is honoured by ``build_plan``, not bypassed here.

    The plan still *shows* the merge -- an operator should see the proposal with the reason
    it cannot be applied -- but selecting it raises ``SelectionError``, which is what makes
    ``apply_plan`` refuse rather than write to a locked field.
    """
    from metaedit.domain.diff import SelectionError

    plan, _ = merge_similar.plan_for_item(_item(LockedFields=["Genres"]), rules=[_rule()])
    assert plan.locked_fields == ("Genres",)
    with pytest.raises(SelectionError, match="locked"):
        plan.resolve_selection(["Genres"])


# ------------------------------------------------------------------- staleness


def test_a_third_spelling_is_reported_as_stale() -> None:
    """A spelling the preview never saw makes the confirmed groups incomplete."""
    stale = merge_similar._stale_spellings(
        _item(Genres=["hip-hop", "Hip Hop", "HIPHOP", "Rock"]), [_rule()]
    )
    assert stale == ["HIPHOP"]


def test_a_value_outside_the_group_is_not_stale() -> None:
    assert merge_similar._stale_spellings(_item(Genres=["hip-hop", "Rockabilly"]), [_rule()]) == []


# ------------------------------------------------------------------ vocabulary


def test_an_explicit_list_becomes_a_vocabulary() -> None:
    vocabulary = merge_similar.vocabulary_from_lists(["hip-hop", "Hip Hop", "Rock", "hip-hop"])
    assert vocabulary.values["Genres"] == {"hip-hop": 2, "Hip Hop": 1, "Rock": 1}
    assert vocabulary.distinct_values() == 3


def test_an_explicit_list_repeating_a_spelling_weights_it() -> None:
    """A caller's repeat is its way of saying "this is the common one"."""
    vocabulary = merge_similar.vocabulary_from_lists(["Hip Hop", "hip-hop", "hip-hop"])
    result = merge_similar.preview(vocabulary)
    assert [group.representative for group in result.previews["Genres"].groups] == ["hip-hop"]


def test_an_explicit_list_cannot_name_an_unmergeable_field() -> None:
    with pytest.raises(Exception, match="cannot merge values in 'Overview'"):
        merge_similar.vocabulary_from_lists(["a"], field="Overview")


def test_the_preview_stamps_each_group_with_its_field() -> None:
    vocabulary = merge_similar.vocabulary_from_lists(["hip-hop", "Hip Hop", "Rock"])
    body = merge_similar.preview(vocabulary).as_dict()
    assert body["groups"][0]["field"] == "Genres"
    assert body["groups"][0]["merge"] == {"hip-hop": "Hip Hop"}
    assert body["scanned"] is None, "an explicit list read nothing"


def test_the_preview_never_offers_a_job_id() -> None:
    """A preview writes nothing, so there is nothing to confirm or undo."""
    body = merge_similar.preview(
        merge_similar.vocabulary_from_lists(["hip-hop", "Hip Hop"])
    ).as_dict()
    assert "job_id" not in body
    assert "batch_id" not in body


def test_the_preview_passes_keep_and_exceptions_through() -> None:
    vocabulary = merge_similar.vocabulary_from_lists(["hip-hop", "Hip Hop"])
    held = merge_similar.preview(vocabulary, keep=["hip-hop"]).as_dict()
    assert held["groups"] == []
    assert len(held["withheld"]) == 1

    narrowed = merge_similar.preview(vocabulary, exceptions=["HipHop"]).as_dict()
    assert len(narrowed["hints"]) == 1
    assert "matches no value" in narrowed["hints"][0]
