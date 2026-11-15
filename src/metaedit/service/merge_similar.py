"""Merging similarly-spelled genres, as a preview and then a confirmed batch.

``domain.similarity`` decides *which* spellings are one genre. This module decides what that
means for the library, and it is built so that nothing is ever merged automatically:

* **Nothing here writes on the strength of the rule.** A preview groups values and reports
  the canonical spelling it would choose; it never proposes a change to a job and never
  reaches Jellyfin's write endpoint (``tests/integration/test_similarity_api.py`` asserts
  that a preview produces no write at all).
* **A merge needs a second request that names the groups it confirms.** The diff and the
  apply both take the confirmed groups in the body, and the service refuses a group the
  similarity rule would not have produced. Confirmation is therefore consent to *that*
  proposal, not to whatever the rule computes next time it runs.
* **The write itself goes through ``bulk.apply_job``.** A merge is expressed as ordinary
  ``DiffPlan``s with a ``replace`` change per touched field, so it inherits -- without
  restating -- snapshot-before-write (ADR 0004), the complete payload (ADR 0003), the etag
  check (ADR 0007), an audit row, per-item failure isolation and batch revert. Writing a
  second apply loop for this would be a second place for those guarantees to stop holding.

Why a diff and an apply rather than a single "merge now" call: the guarantee the rest of the
bulk surface holds is that *a job must exist before a write*, and the job id is what proves
a preview of these exact groups happened. A one-shot endpoint would have to re-derive the
groups and write in the same breath, which is precisely the automatic behaviour this feature
must not have.

Scope, stated honestly: the apply merges **within a selection of items** (the same
kind/parent/search/ids/artist/album/exclude narrowings every batch accepts). It is not a
library-wide rename, because the write path is per item and bounded by ``MAX_BATCH_ITEMS``;
running it over a whole media type is a selection with a large ``limit``, which the operator
still has to review as one batch.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from metaedit.adapters.jellyfin.client import JellyfinClient
from metaedit.domain.browse_filters import MAX_SCAN_ITEMS, SCAN_PAGE_SIZE
from metaedit.domain.diff import DiffPlan, build_plan
from metaedit.domain.errors import NotFoundError, ValidationError
from metaedit.domain.genre_removal import normalise
from metaedit.domain.mapping import Candidate, FieldChange, MappingResult
from metaedit.domain.similarity import (
    SimilarityPreview,
    build_preview,
    rewrite_groups,
    squash_key,
)
from metaedit.domain.snapshot import NormalizedItem, from_dto
from metaedit.logging import get_logger
from metaedit.service.bulk import MAX_BATCH_ITEMS, BulkItem
from metaedit.service.genre_removal import operator_confidence, placeholder_plan
from metaedit.service.planning import ITEM_KIND_BY_QUERY, item_kind_for
from metaedit.service.selection import Selection, SelectionOutcome

log = get_logger(__name__)

# The arrays a merge may act on. A closed vocabulary, and it is not a choice offered to the
# caller for the *values*: the groups name the field they were found in, and the apply
# derives its field set from those groups. Declared so a derived name can never be a field
# this application does not write.
MERGE_FIELDS: tuple[str, ...] = ("Genres", "Tags")


# ----------------------------------------------------------------------- vocabulary


@dataclass(frozen=True, slots=True)
class Vocabulary:
    """The genre/style values found, and how often each occurs, per field.

    ``values`` is keyed by field and then by the spelling *as stored*, because the
    representative is chosen by which spelling the library already uses most -- a count per
    normalised value would let ``hip hop`` and ``Hip Hop`` outvote a third spelling together
    while being, to a case-insensitive genre list, the same one.

    The per-field split is not cosmetic: ``Genres`` is what Jellyfin navigates by and
    ``Tags`` is where the rest of the Last.fm tags go (ADR 0006), so the two hold different
    vocabularies and a group is only ever within one field. Merging a ``Genres`` spelling
    into a ``Tags`` value would be a cross-field edit no operator asked for.
    """

    source: str
    fields: tuple[str, ...]
    values: Mapping[str, Mapping[str, int]]
    # How many items were read, and whether the read stopped at the cap. ``None`` for an
    # explicit list, where there was nothing to read.
    scanned: int | None = None
    truncated: bool = False

    def distinct_values(self) -> int:
        return sum(len(spellings) for spellings in self.values.values())


def vocabulary_from_lists(values: Iterable[str], *, field: str = "Genres") -> Vocabulary:
    """A vocabulary from an explicit list, for a caller that already has the values.

    Counts are one per entry, so a list that repeats a spelling weights it -- which is the
    caller's way of saying "this is the common one". The default field is ``Genres`` because
    an explicit list of genre values is the common case, and the endpoint lets a caller name
    ``Tags`` when it is not.
    """
    if field not in MERGE_FIELDS:
        raise ValidationError(
            f"cannot merge values in {field!r}; expected one of {list(MERGE_FIELDS)}"
        )
    counts: dict[str, int] = {}
    for raw in values:
        spelling = (raw or "").strip()
        if spelling:
            counts[spelling] = counts.get(spelling, 0) + 1
    return Vocabulary(source="explicit list", fields=(field,), values={field: counts})


async def read_vocabulary(
    client: JellyfinClient,
    *,
    kind: str,
    parent_id: str | None = None,
    fields: Sequence[str] = MERGE_FIELDS,
) -> Vocabulary:
    """Read a media type's genre and style values, counting occurrences.

    A scan, and unavoidably so. Jellyfin's ``/Genres`` endpoint lists genre *entities*,
    which is the canonical list a merge is trying to reduce rather than the raw stored
    values it has to reconcile -- and it says nothing at all about ``Tags``. Reading the
    items is the only way to see what is actually stored, so it is bounded by
    ``MAX_SCAN_ITEMS`` and reports whether it stopped at the cap. A truncated read makes the
    preview a lower bound, and saying so is what lets an operator decide whether to narrow
    the media type.

    Read-only: one paged browse per preview, and no writes anywhere.
    """
    item_kind = ITEM_KIND_BY_QUERY.get(kind)
    if item_kind is None:
        raise ValidationError(
            f"unknown kind {kind!r}; expected one of {sorted(ITEM_KIND_BY_QUERY)}"
        )
    wanted = [name for name in fields if name in MERGE_FIELDS]
    if not wanted:
        raise ValidationError(f"nothing to merge in {list(fields)}; expected {list(MERGE_FIELDS)}")

    counts: dict[str, dict[str, int]] = {name: {} for name in wanted}
    scanned = 0
    truncated = True
    while scanned < MAX_SCAN_ITEMS:
        page = await client.items(
            kind=item_kind,
            parent_id=parent_id,
            start_index=scanned,
            limit=min(SCAN_PAGE_SIZE, MAX_SCAN_ITEMS - scanned),
        )
        if not page.Items:
            truncated = False
            break
        for dto in page.Items:
            scanned += 1
            for name in wanted:
                for value in getattr(dto, name, None) or []:
                    spelling = str(value).strip()
                    if spelling:
                        counts[name][spelling] = counts[name].get(spelling, 0) + 1
        if scanned >= (page.TotalRecordCount or 0):
            truncated = False
            break
    return Vocabulary(
        source=f"library ({kind})",
        fields=tuple(wanted),
        values=counts,
        scanned=scanned,
        truncated=truncated,
    )


# -------------------------------------------------------------------------- preview


@dataclass(frozen=True, slots=True)
class PreviewResult:
    """One preview response: a preview per field, and what was read to build it."""

    vocabulary: Vocabulary
    previews: Mapping[str, SimilarityPreview]

    def as_dict(self) -> dict[str, Any]:
        """The response body. Every group is stamped with the field it was found in.

        The stamping happens here rather than in the UI because the apply needs it: a
        confirmed group that named no field would be ambiguous about which array to rewrite.
        """
        return {
            "source": self.vocabulary.source,
            "distinct_values": self.vocabulary.distinct_values(),
            "scanned": self.vocabulary.scanned,
            "truncated": self.vocabulary.truncated,
            "groups": [
                {**group.as_dict(), "field": name}
                for name, preview in self.previews.items()
                for group in preview.groups
            ],
            "withheld": [
                {
                    **group.as_dict(),
                    "field": name,
                    "reason": "held back by an exception, or narrowed to a single spelling",
                }
                for name, preview in self.previews.items()
                for group in preview.withheld
            ],
            "hints": [hint for preview in self.previews.values() for hint in preview.hints],
        }


def preview(
    vocabulary: Vocabulary,
    *,
    keep: Iterable[str] = (),
    exceptions: Iterable[str] = (),
) -> PreviewResult:
    """Group every field's values and describe the merge a confirmation would perform.

    ``keep``/``exceptions`` only narrow the proposal; they never widen it, and an exception
    that matches nothing is reported in ``hints`` rather than dropped -- see
    ``domain.similarity.build_preview``.
    """
    previews = {
        name: build_preview(
            spellings,
            counts=spellings,
            source=vocabulary.source,
            scanned=vocabulary.scanned,
            truncated=vocabulary.truncated,
            keep=keep,
            exceptions=exceptions,
        )
        for name, spellings in vocabulary.values.items()
    }
    return PreviewResult(vocabulary=vocabulary, previews=previews)


# -------------------------------------------------------------------------- rules


@dataclass(frozen=True, slots=True)
class MergeRule:
    """One confirmed rewrite: every spelling of a group becomes ``representative``.

    ``spellings`` is the whole group as the preview reported it, so the rewrite cannot reach
    a value that was not proposed. ``squashed`` is derived, never supplied, so a caller
    cannot claim a group whose members do not actually share a key.
    """

    field: str
    representative: str
    spellings: tuple[str, ...]

    @property
    def squashed(self) -> str:
        return squash_key(self.representative)

    @property
    def keys(self) -> frozenset[str]:
        """The ``normalize_tag`` keys of the spellings this rule rewrites."""
        return frozenset(normalise(value) for value in self.spellings)

    def as_dict(self) -> dict[str, Any]:
        return {
            "field": self.field,
            "representative": self.representative,
            "spellings": list(self.spellings),
            "squashed": self.squashed,
        }


def build_rules(groups: Iterable[Mapping[str, Any]]) -> tuple[MergeRule, ...]:
    """Validate a confirmed group list from a request body.

    Every group is re-checked against the rule that produced it, so this endpoint cannot be
    turned into a general-purpose rename: a body naming ``Rock`` -> ``Rockabilly`` describes
    a group the similarity rule would never have produced, and is refused. That matters
    because "confirm what you were shown" is the whole safety model here -- a confirmation
    that can name its own proposal is not one.

    An empty list is refused too: an apply with nothing confirmed is a request to merge
    everything, and this feature never merges anything automatically.
    """
    rules: list[MergeRule] = []
    for group in groups:
        field_name = str(group.get("field", "")).strip()
        representative = str(group.get("representative", "")).strip()
        spellings = tuple(str(value).strip() for value in group.get("spellings") or ())
        if field_name not in MERGE_FIELDS:
            raise ValidationError(
                f"cannot merge values in {field_name!r}; expected one of {list(MERGE_FIELDS)}"
            )
        if not representative or len(spellings) < 2:
            raise ValidationError(
                "a merge group needs a representative and at least two spellings; run the "
                "preview again and confirm what it reported"
            )
        key = squash_key(representative)
        if not key or any(squash_key(value) != key for value in spellings):
            raise ValidationError(
                f"the group for {representative!r} contains a value that is not the same "
                "genre spelled differently, so similarity would not merge it"
            )
        if len({normalise(value) for value in spellings}) < 2:
            raise ValidationError(
                f"the group for {representative!r} is one spelling repeated, so there is "
                "nothing to merge"
            )
        if normalise(representative) not in {normalise(value) for value in spellings}:
            raise ValidationError(
                f"the group for {representative!r} does not contain its own representative; "
                "a merge rewrites spellings of a value, it does not invent one"
            )
        rules.append(
            MergeRule(field=field_name, representative=representative, spellings=spellings)
        )
    if not rules:
        raise ValidationError(
            "no groups were confirmed. A merge never runs automatically: preview it, then "
            "name the groups you accept."
        )
    return tuple(rules)


# ------------------------------------------------------------------------ selection


@dataclass(slots=True)
class MergeSelection:
    """Where a confirmed merge may write, and how far.

    The same narrowings every batch accepts. There is no "select by value" here: an item is
    in scope because one of its fields holds a confirmed spelling, which is computed rather
    than queried -- see :func:`find_items`.
    """

    kind: str = "artist"
    parent_id: str | None = None
    search: str | None = None
    ids: list[str] | None = None
    limit: int = 100
    artist_ids: tuple[str, ...] = ()
    album_ids: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()

    def filter_for(self) -> Selection:
        return Selection.build(
            kind=self.kind,
            artist_ids=self.artist_ids,
            album_ids=self.album_ids,
            exclude=self.exclude,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "parent_id": self.parent_id,
            "search": self.search,
            "limit": self.limit,
            "artist_ids": list(self.artist_ids),
            "album_ids": list(self.album_ids),
            "exclude": list(self.exclude),
        }


def _branch(rules: Sequence[MergeRule]) -> dict[str, frozenset[str]]:
    """Per field, the set of ``normalize_tag`` keys this merge may rewrite."""
    grouped: dict[str, set[str]] = {}
    for rule in rules:
        grouped.setdefault(rule.field, set()).update(rule.keys)
    return {name: frozenset(keys) for name, keys in grouped.items()}


def _holds_any(dto: Any, branch: Mapping[str, frozenset[str]]) -> bool:
    """Whether an item holds any spelling this merge would rewrite."""
    for name, keys in branch.items():
        for value in getattr(dto, name, None) or []:
            if normalise(str(value)) in keys:
                return True
    return False


async def find_items(
    client: JellyfinClient,
    *,
    merge_selection: MergeSelection,
    rules: Sequence[MergeRule],
) -> SelectionOutcome:
    """The items holding one of the confirmed spellings, and what reading them cost.

    Read rather than forwarded. Jellyfin can filter ``Genres=one value`` exactly, but it
    cannot ask for "any of these spellings, in either of two arrays", and it *ands* the
    parameters it is given (ADR 0015), so two queries would return the wrong set in both
    directions. So the media type is read and each item tested locally, bounded by
    ``MAX_SCAN_ITEMS`` with ``truncated`` reported.

    An explicit id list is honoured and still tested: the caller may name an item that holds
    none of the spellings, and writing a "merge" to it would be a no-op the report would
    have to explain.
    """
    if merge_selection.limit > MAX_BATCH_ITEMS:
        raise ValidationError(
            f"limit {merge_selection.limit} exceeds the {MAX_BATCH_ITEMS} item batch cap; "
            "narrow the selection or raise the cap deliberately"
        )
    wanted = merge_selection.filter_for()
    item_kind = ITEM_KIND_BY_QUERY.get(merge_selection.kind)
    if item_kind is None:
        raise ValidationError(
            f"unknown selection kind {merge_selection.kind!r}; expected one of "
            f"{sorted(ITEM_KIND_BY_QUERY)}"
        )
    branch = _branch(rules)

    explicit = [item_id for item_id in (merge_selection.ids or []) if item_id]
    if explicit:
        candidates = list(await client.items_by_ids(explicit[: merge_selection.limit]))
        kept = [dto for dto in candidates if wanted.carries(dto) and _holds_any(dto, branch)]
        return SelectionOutcome(
            items=kept,
            scanned=len(candidates),
            excluded=len(candidates) - len(kept),
        )

    facets = wanted.jellyfin_params()
    cap = max(min(MAX_SCAN_ITEMS, max(merge_selection.limit, 1)), 1)
    matched: list[Any] = []
    scanned = 0
    excluded = 0
    truncated = True
    while scanned < cap and len(matched) < merge_selection.limit:
        page = await client.items(
            kind=item_kind,
            parent_id=merge_selection.parent_id,
            search_term=merge_selection.search,
            start_index=scanned,
            limit=min(SCAN_PAGE_SIZE, cap - scanned),
            filters=facets,
        )
        if not page.Items:
            truncated = False
            break
        for dto in page.Items:
            scanned += 1
            if not wanted.carries(dto):
                excluded += 1
                continue
            if _holds_any(dto, branch):
                matched.append(dto)
                if len(matched) >= merge_selection.limit:
                    break
        if len(matched) >= merge_selection.limit:
            # The selection is full: the requested limit was honoured, not a partial read.
            truncated = False
            break
        if scanned >= (page.TotalRecordCount or 0):
            truncated = False
            break
    return SelectionOutcome(
        items=matched[: merge_selection.limit],
        scanned=scanned,
        excluded=excluded,
        truncated=truncated,
    )


# ------------------------------------------------------------------------- diffing


def plan_for_item(
    item: NormalizedItem, *, rules: Sequence[MergeRule]
) -> tuple[DiffPlan, dict[str, list[int]]]:
    """Build a ``replace`` plan that rewords the confirmed spellings on one item.

    A genuine ``DiffPlan`` rather than a bespoke payload, so the write goes through
    ``apply_plan`` and keeps every guarantee the rest of the application's writes have. The
    second return value says which positions of each field changed, for the stream -- a
    ``DiffPlan`` reports what the field became, not that ``hip-hop`` specifically became
    ``Hip Hop``, and that difference is the whole point of the feature.
    """
    changes: list[FieldChange] = []
    rewritten: dict[str, list[int]] = {}

    # One field is rewritten in **one pass**, with every group's replacements applied
    # together. Calling the rewrite once per rule would run the second group over the
    # first's output, and a field change per rule would mean two `FieldChange`s for one
    # field, which `resolve_selection` then cannot address coherently.
    for field_name in _fields_in_order(rules):
        rules_for_field = [rule for rule in rules if rule.field == field_name]
        replacement = {
            normalise(spelling): rule.representative
            for rule in rules_for_field
            for spelling in rule.spellings
        }
        current = [str(value) for value in (item.get(field_name) or [])]
        after, positions = rewrite_groups(current, groups=[replacement])
        if not positions:
            continue
        rewritten[field_name] = positions
        described = ", ".join(repr(rule.representative) for rule in rules_for_field)
        changes.append(
            FieldChange(
                field=field_name,
                current=current,
                proposed=after,
                mode="replace",
                reason=f"rewrites {len(positions)} value(s) to {described} in {field_name}",
                source="operator.similarity",
                # Pre-selected: the operator confirmed these groups by naming them, so
                # making them tick it again would be ceremony rather than review.
                selected=True,
                provenance={
                    "source": "operator",
                    "similarity": [rule.as_dict() for rule in rules_for_field],
                },
            )
        )

    mapping = MappingResult(changes=changes, skipped=[])
    plan = build_plan(
        item=item,
        candidate=Candidate(kind=item.kind, name=item.name),
        # Not a Last.fm match, so there is no match to score; the operator's confirmation is
        # the authority. Same reasoning as the removal feature's operator confidence.
        confidence=operator_confidence(),
        mapping=mapping,
        locked_fields=item.get("LockedFields") or [],
    )
    return plan, rewritten


def _fields_in_order(rules: Sequence[MergeRule]) -> list[str]:
    """:data:`MERGE_FIELDS` order rather than group order, so a diff's field order is stable."""
    present = {rule.field for rule in rules}
    return [name for name in MERGE_FIELDS if name in present]


@dataclass(slots=True)
class MergeJob:
    """A reviewed merge run, held in memory between the diff and the apply.

    Structurally satisfies ``bulk.ApplicableJob`` -- same field names, ``applied`` mutable --
    so ``bulk.apply_job`` can write it without knowing what a similarity merge is.
    """

    job_id: str
    batch_id: str
    created_at: datetime
    requested: dict[str, Any]
    rules: tuple[MergeRule, ...]
    items: list[BulkItem] = field(default_factory=list)
    # What each item's merge rewrote, keyed by item id: ``{field: [changed indexes]}``.
    rewritten: dict[str, dict[str, list[int]]] = field(default_factory=dict)
    applied: bool = False
    report: SelectionOutcome | None = None

    @property
    def applicable_items(self) -> list[BulkItem]:
        return [item for item in self.items if item.applicable]

    def summary(self) -> dict[str, Any]:
        summary: dict[str, Any] = {
            "job_id": self.job_id,
            "batch_id": self.batch_id,
            "items": len(self.items),
            "applicable": len(self.applicable_items),
            "skipped": len(self.items) - len(self.applicable_items),
            "created_at": self.created_at.isoformat(),
            "applied": self.applied,
            "excluded": self.report.excluded if self.report is not None else 0,
            # What the run merges, group by group: "merged 12 items" does not say whether
            # it renamed `hip-hop` to `Hip Hop` or the reverse, and that is the thing the
            # operator just agreed to.
            "merging": [
                {"field": rule.field, "to": rule.representative, "from": list(rule.spellings)}
                for rule in self.rules
            ],
            "rewritten_values": sum(
                len(indexes) for fields in self.rewritten.values() for indexes in fields.values()
            ),
        }
        if self.report is not None:
            summary["scanned"] = self.report.scanned
            summary["truncated"] = self.report.truncated
        return summary


class MergeJobRegistry:
    """Bounded, process-local store of reviewed merge runs.

    Its own registry for the reason the other batches have theirs: a job id must not be
    applicable through an endpoint that assembles a different kind of write.
    """

    def __init__(self, *, max_jobs: int = 50) -> None:
        self._jobs: dict[str, MergeJob] = {}
        self._order: list[str] = []
        self._max_jobs = max_jobs

    def add(self, job: MergeJob) -> None:
        self._jobs[job.job_id] = job
        self._order.append(job.job_id)
        while len(self._order) > self._max_jobs:
            self._jobs.pop(self._order.pop(0), None)

    def get(self, job_id: str) -> MergeJob | None:
        return self._jobs.get(job_id)

    def list(self) -> list[MergeJob]:
        return [self._jobs[job_id] for job_id in self._order if job_id in self._jobs]

    def clear(self) -> None:
        """Test hook."""
        self._jobs.clear()
        self._order.clear()


_merge_registry = MergeJobRegistry()


def merge_registry() -> MergeJobRegistry:
    return _merge_registry


def _stale_spellings(item: NormalizedItem, rules: Sequence[MergeRule]) -> list[str]:
    """Values in a group's field that squash to its key but were not in the confirmed group.

    A new spelling -- added in Jellyfin since the preview, or on an item the preview's scan
    never reached -- makes the confirmed groups incomplete. Merging the known ones anyway
    would leave a half-merged group behind, so the item is skipped with a reason and the
    operator can preview again. Refusing to *act dishonestly* is the point: the alternative
    is a merge that reports success while the library still has a third spelling.
    """
    keys = {rule.squashed for rule in rules}
    known = {normalise(value) for rule in rules for value in rule.spellings}
    stale: list[str] = []
    for name in {rule.field for rule in rules}:
        for value in item.get(name) or []:
            if squash_key(str(value)) in keys and normalise(str(value)) not in known:
                stale.append(str(value))
    return stale


async def build_job(
    *,
    client: JellyfinClient,
    merge_selection: MergeSelection,
    groups: Iterable[Mapping[str, Any]],
) -> MergeJob:
    """Diff a confirmed merge over a selection. **Writes nothing.**

    The confirmed groups are in the body, which is what makes this a review rather than an
    automatic pass: a request with no groups is refused, and every group is re-checked
    against the similarity rule.
    """
    rules = build_rules(groups)
    outcome = await find_items(client, merge_selection=merge_selection, rules=rules)
    job = MergeJob(
        job_id=uuid.uuid4().hex,
        batch_id=uuid.uuid4().hex,
        created_at=datetime.now(UTC),
        requested={**merge_selection.as_dict(), "groups": [rule.as_dict() for rule in rules]},
        rules=rules,
        report=outcome,
    )

    for dto in outcome.items:
        try:
            kind = item_kind_for(dto)
            item = from_dto(dto.model_dump(), kind)
        except Exception as exc:
            # One unreadable DTO is one item's problem; raising would abandon the run with
            # no record of which item was at fault.
            job.items.append(
                BulkItem(
                    item_id=getattr(dto, "Id", "") or "",
                    name=getattr(dto, "Name", "") or "",
                    kind=getattr(dto, "Type", "") or "",
                    plan=placeholder_plan(dto),
                    skipped_reason=f"could not read this item: {type(exc).__name__}",
                )
            )
            continue

        stale = _stale_spellings(item, rules)
        plan, rewritten = plan_for_item(item, rules=rules)
        if stale and plan.proposed:
            job.items.append(
                BulkItem(
                    item_id=item.item_id,
                    name=item.name,
                    kind=item.kind,
                    plan=plan,
                    skipped_reason=(
                        f"holds {', '.join(sorted(stale))}, a spelling this preview did not "
                        "group; preview again before merging"
                    ),
                )
            )
            continue

        job.rewritten[item.item_id] = rewritten
        job.items.append(
            BulkItem(
                item_id=item.item_id,
                name=item.name,
                kind=item.kind,
                plan=plan,
                skipped_reason=None if plan.proposed else "already uses the canonical spelling",
            )
        )

    _merge_registry.add(job)
    log.info("merge_job_built", **job.summary())
    return job


async def stream_job_diff(job: MergeJob) -> AsyncIterator[dict[str, Any]]:
    """Yield the reviewed rewrite for each item, one event at a time.

    The frame shape mirrors ``genre_removal.stream_job_diff`` -- ``diff`` plus a per-field
    report under the ``removals`` key -- so one client renders every reviewed batch. The
    rewrites ride under ``removals`` and a merge-specific ``merge`` map is added alongside,
    because a removal and a merge both answer "this value becomes that one" and neither
    needs a second renderer for a difference that is only vocabulary.
    """
    for index, item in enumerate(job.items):
        yield {
            "type": "item",
            "index": index,
            "item_id": item.item_id,
            "name": item.name,
            "kind": item.kind,
            "skipped_reason": item.skipped_reason,
            "applicable": item.applicable,
            "diff": item.plan.as_dict(),
            "removals": [
                merge_view(change, job.rewritten.get(item.item_id, {}).get(change.field, []))
                for change in item.plan.mapping.changes
            ],
            "merge": job.rewritten.get(item.item_id, {}),
        }
    yield {"type": "summary", **job.summary(), "emptied": []}


def merge_view(change: FieldChange, changed: list[int]) -> dict[str, Any]:
    """One rewording of one field, in the shape a removal outcome's frame has.

    ``to`` is the representatives this field's groups converged on, joined -- a field can
    carry two groups at once, and naming only one of them would describe half the write.
    The frame keeps the removal's key names so one client renders both batches; ``to`` is a
    string where a removal would have ``target`` because a merge has a destination rather
    than something being deleted.
    """
    rules = change.provenance.get("similarity") or []
    representatives = [rule.get("representative") for rule in rules if isinstance(rule, dict)]
    return {
        "field": change.field,
        "to": ", ".join(str(name) for name in representatives if name) or None,
        "before": list(change.current or []),
        "after": list(change.proposed or []),
        "changed": list(changed),
    }


async def apply_merge_job(
    *,
    session: AsyncSession,
    client: JellyfinClient,
    job: MergeJob,
    selections: dict[str, list[str]] | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """Apply a reviewed merge, isolating failures per item.

    Delegates to ``bulk.apply_job``, exactly as the removal feature does: one write path,
    one snapshotted write per item, one batch-revert mechanism. The job satisfies
    ``ApplicableJob`` structurally, so nothing about the shared path had to change.

    ``selections`` narrows *which fields* write per item, as it does for every batch. It
    cannot name a field outside the confirmed groups: ``DiffPlan.resolve_selection`` refuses
    fields the plan does not propose, which is the same rule that makes a removal unable to
    clear a field it did not plan.
    """
    from metaedit.service.bulk import apply_job

    if selections is not None:
        touched = {rule.field for rule in job.rules}
        named = {field for fields in selections.values() for field in fields}
        unknown = named - touched
        if unknown:
            raise ValidationError(
                f"a merge writes only the fields its groups name ({', '.join(sorted(touched))}); "
                f"refusing {', '.join(sorted(unknown))}"
            )

    async for event in apply_job(
        session=session,
        client=client,
        job=job,
        selections=selections,
    ):
        yield event


async def get_merge_job_or_404(job_id: str) -> MergeJob:
    job = _merge_registry.get(job_id)
    if job is None:
        raise NotFoundError(
            f"Unknown or expired merge job {job_id!r}. Jobs are held in memory and a "
            "restart clears them; run the diff again."
        )
    return job


__all__ = [
    "MERGE_FIELDS",
    "MergeJob",
    "MergeRule",
    "MergeSelection",
    "PreviewResult",
    "Vocabulary",
    "apply_merge_job",
    "build_job",
    "build_rules",
    "find_items",
    "get_merge_job_or_404",
    "merge_registry",
    "plan_for_item",
    "preview",
    "read_vocabulary",
    "stream_job_diff",
    "vocabulary_from_lists",
]
