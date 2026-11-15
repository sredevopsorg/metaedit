"""Clearing every tag from many items, keeping an operator's exceptions.

"Remove all tags, allowing exceptions" is the request, and it is one reviewed batch in the
same sense as :mod:`metaedit.service.genre_removal`: a diff that writes nothing, a job
that must exist before an apply, per-item failure isolation, and a shared ``batch_id`` so
the whole run reverts as one unit. The write path is ``bulk.apply_job`` -- there is not a
second one, deliberately. A separate loop would be a second place for the snapshot, the
payload-completeness rule (ADR 0003), the etag check (ADR 0007) and the audit row to
quietly stop holding.

What is genuinely different from a single-genre removal is *selection*. That feature can
ask Jellyfin for "items carrying genre X", which is an exact server-side filter. This one
cannot: the target is "everything that is not on the keep list", and Jellyfin has no way to
express it. So the selection is read item by item, bounded by ``MAX_SCAN_ITEMS``, and the
report says how many items were read and whether the read stopped at the cap. A truncated
scan is a batch that covered part of the selection, and saying so is the difference between
an operator's decision and a surprise.

Three safety facts, stated here rather than left to be inferred:

* **An empty exception list clears everything in the selection.** That is the intended use
  and the most destructive operation in the application, so the diff summary reports how
  many items and how many values it would empty instead of merely counting changes.
* **An exception that matches nothing is reported.** Matching is exact and
  case-insensitive (ADR 0015), so a typo keeps nothing -- and a keep list that silently
  keeps nothing is indistinguishable from one that worked until the tags are gone.
* **An exception is never removed.** The keep set is applied as a positive test on every
  value, and a unit test asserts the property directly: for any stored value and any
  exception list, no value whose normalised form is in the list can appear in ``removed``.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from metaedit.adapters.jellyfin.client import JellyfinClient
from metaedit.domain.browse_filters import MAX_SCAN_ITEMS, SCAN_PAGE_SIZE
from metaedit.domain.diff import DiffPlan, build_plan
from metaedit.domain.errors import NotFoundError, ValidationError
from metaedit.domain.genre_removal import (
    REMOVAL_FIELDS,
    ClearOutcome,
    keep_set,
    normalise,
    plan_clears,
    split_kept_component,
)
from metaedit.domain.mapping import Candidate, FieldChange, MappingResult
from metaedit.domain.snapshot import NormalizedItem, from_dto
from metaedit.logging import get_logger
from metaedit.service.bulk import MAX_BATCH_ITEMS, BulkItem
from metaedit.service.genre_removal import operator_confidence, placeholder_plan
from metaedit.service.planning import ITEM_KIND_BY_QUERY, item_kind_for
from metaedit.service.selection import Selection, SelectionOutcome

log = get_logger(__name__)

# The field lists this operation may act on, in the order a diff reports them. Reusing the
# removal module's closed set rather than declaring a second one: these are the two arrays
# this application writes, and a third list would be a third thing to keep in step.
DEFAULT_FIELDS: tuple[str, ...] = ("Tags",)


def _fields_for(requested: Iterable[str] | None) -> tuple[str, ...]:
    """Validate the requested fields against the closed set, defaulting to ``Tags``.

    ``Tags`` by default and not both arrays. ``Genres`` is what Jellyfin navigates and
    filters by (ADR 0006); clearing it is a legitimate edit and available through the same
    ``fields`` parameter, but it is not what "remove all tags" means, so an operator has to
    say so. Emptying the genre list by default would be the largest possible surprise for
    a request that named tags.
    """
    if not requested:
        return DEFAULT_FIELDS
    fields: list[str] = []
    for name in requested:
        if name not in REMOVAL_FIELDS:
            raise ValidationError(f"cannot clear {name!r}; expected one of {list(REMOVAL_FIELDS)}")
        if name not in fields:
            fields.append(name)
    # Stable order, so a diff's field order does not depend on how the body was written.
    return tuple(name for name in REMOVAL_FIELDS if name in fields)


def _exceptions(exceptions: Iterable[str] | None) -> tuple[str, ...]:
    """Validate the keep list before anything is read.

    ``keep_set`` refuses a blank entry; this wrapper only strips the typed spellings and
    drops repeats, so the job records what the operator actually asked to keep rather than
    a normalised rendering of it. Duplicates collapse because two spellings of one
    exception are one instruction, and keeping both would make the unmatched-exception
    report name the same value twice.
    """
    keys = keep_set(exceptions)
    typed: list[str] = []
    seen: set[str] = set()
    for entry in exceptions or ():
        key = normalise(entry)
        if key in keys and key not in seen:
            seen.add(key)
            typed.append(entry.strip())
    return tuple(typed)


@dataclass(slots=True)
class ClearSelection:
    """Where to look, and what to keep.

    The same narrowings as ``genre_removal.RemovalSelection`` -- parent, search, explicit
    ids, the artist/album facets, exclusion patterns -- minus the target, because there is
    no value to select *by*. ``limit`` bounds how many items the batch will write to;
    ``scan_cap`` bounds how many it will read to decide, which is a different question and
    is why it is a separate number.
    """

    kind: str = "artist"
    parent_id: str | None = None
    search: str | None = None
    ids: list[str] | None = None
    limit: int = 100
    fields: tuple[str, ...] = DEFAULT_FIELDS
    exceptions: tuple[str, ...] = ()
    decompose: bool = False
    artist_ids: tuple[str, ...] = ()
    album_ids: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()

    def filter_for(self) -> Selection:
        """The shared interpretation of the narrowings, validated on the way in."""
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
            "fields": list(self.fields),
            "exceptions": list(self.exceptions),
            "decompose": self.decompose,
            "artist_ids": list(self.artist_ids),
            "album_ids": list(self.album_ids),
            "exclude": list(self.exclude),
        }


@dataclass(slots=True)
class ClearJob:
    """A reviewed clear-everything run, held in memory between the diff and the apply.

    Structurally satisfies ``bulk.ApplicableJob``, exactly as ``RemovalJob`` does, so both
    share one write path. A distinct type because the summary has to say things a Last.fm
    diff has no concept of: what is being kept, how many values the run would delete, and
    which exceptions matched nothing.
    """

    job_id: str
    batch_id: str
    created_at: datetime
    requested: dict[str, Any]
    items: list[BulkItem] = field(default_factory=list)
    outcomes: dict[str, list[ClearOutcome]] = field(default_factory=dict)
    applied: bool = False
    report: SelectionOutcome | None = None

    @property
    def applicable_items(self) -> list[BulkItem]:
        return [item for item in self.items if item.applicable]

    @property
    def emptied(self) -> list[dict[str, str]]:
        """Items where a field would be left with nothing at all.

        The load-bearing report for this feature. "Clear every tag" empties almost
        everything it touches, and an operator confirming it should see the count rather
        than deduce it from a run of per-item frames.
        """
        found: list[dict[str, str]] = []
        for item in self.items:
            for outcome in self.outcomes.get(item.item_id, []):
                if outcome.emptied:
                    found.append(
                        {"item_id": item.item_id, "name": item.name, "field": outcome.field}
                    )
        return found

    def removed_values(self) -> int:
        """How many stored values the whole run would delete."""
        return sum(
            len(outcome.removed) for outcomes in self.outcomes.values() for outcome in outcomes
        )

    def matched_exceptions(self) -> set[str]:
        """The keep-list keys at least one selected item actually carries."""
        matched: set[str] = set()
        for outcomes in self.outcomes.values():
            for outcome in outcomes:
                matched.update(outcome.kept_exceptions())
        return matched

    def unmatched_exceptions(self) -> list[str]:
        """Exceptions that keep nothing anywhere in this selection.

        Reported, never silently dropped: a misspelt exception removes the value it was
        meant to protect, so the operator must be told before confirming rather than
        discovering it in the library afterwards.
        """
        matched = self.matched_exceptions()
        return [
            typed
            for typed in self.requested.get("exceptions", [])
            if normalise(typed) not in matched
        ]

    def summary(self) -> dict[str, Any]:
        summary: dict[str, Any] = {
            "job_id": self.job_id,
            "batch_id": self.batch_id,
            "items": len(self.items),
            "applicable": len(self.applicable_items),
            "skipped": len(self.items) - len(self.applicable_items),
            "created_at": self.created_at.isoformat(),
            "applied": self.applied,
            "clearing": self.fields_requested(),
            "keeping": list(self.requested.get("exceptions", [])),
            "unmatched_exceptions": self.unmatched_exceptions(),
            "removed_values": self.removed_values(),
            "excluded": self.report.excluded if self.report is not None else 0,
        }
        if self.report is not None and self.report.scanned is not None:
            # This feature always reads the library, so these are always present -- unlike
            # a forwarded bulk selection, where their absence is a statement of its own.
            summary["scanned"] = self.report.scanned
            summary["truncated"] = self.report.truncated
        return summary

    def fields_requested(self) -> list[str]:
        """The fields this job clears, as recorded on the request."""
        raw = self.requested.get("fields")
        return [str(name) for name in raw] if isinstance(raw, list) else list(DEFAULT_FIELDS)


class ClearJobRegistry:
    """Bounded, process-local store of reviewed clear runs.

    Separate from the bulk and removal registries for the same reason those two are
    separate from each other: an id that belongs to one kind of job must be a clear 404 at
    another kind's apply endpoint, not a plan assembled from the wrong shape of job.
    """

    def __init__(self, *, max_jobs: int = 50) -> None:
        self._jobs: dict[str, ClearJob] = {}
        self._order: list[str] = []
        self._max_jobs = max_jobs

    def add(self, job: ClearJob) -> None:
        self._jobs[job.job_id] = job
        self._order.append(job.job_id)
        while len(self._order) > self._max_jobs:
            self._jobs.pop(self._order.pop(0), None)

    def get(self, job_id: str) -> ClearJob | None:
        return self._jobs.get(job_id)

    def list(self) -> list[ClearJob]:
        return [self._jobs[job_id] for job_id in self._order if job_id in self._jobs]

    def clear(self) -> None:
        """Test hook."""
        self._jobs.clear()
        self._order.clear()


_clear_registry = ClearJobRegistry()


def clear_registry() -> ClearJobRegistry:
    return _clear_registry


def plan_for_item(
    item: NormalizedItem,
    *,
    fields: tuple[str, ...],
    exceptions: tuple[str, ...] = (),
    decompose: bool = False,
) -> tuple[DiffPlan, list[ClearOutcome]]:
    """Build a reviewable plan that clears ``fields`` on one item.

    Returns the plan and the per-field outcomes, because the report is what makes this
    operation confirmable at all: the operator needs to see which values would go and
    whether a field would be left empty, not just that the item "would change".
    """
    initial = plan_clears(item.fields, fields=fields, exceptions=exceptions)
    changes: list[FieldChange] = []
    skipped: list[FieldChange] = []
    # Rebuilt rather than mutated for the reason `genre_removal.plan_for_item` spells out:
    # decomposition changes what the outcome *says*, and returning the pre-split version
    # would report a report that disagrees with the write.
    outcomes: list[ClearOutcome] = []

    for outcome in initial:
        proposed = list(outcome.after)
        if decompose:
            proposed, removed = _decomposed(outcome, exceptions=exceptions)
            outcome = ClearOutcome(
                field=outcome.field,
                before=outcome.before,
                after=proposed,
                removed=removed,
                exceptions=outcome.exceptions,
            )
        outcomes.append(outcome)
        change = FieldChange(
            field=outcome.field,
            current=outcome.before,
            proposed=proposed,
            mode="replace",
            reason=_reason(outcome, decompose=decompose),
            source="operator.clear",
            # Pre-selected when it would change something: the request *is* the
            # instruction, so making the operator tick it again would be ceremony.
            selected=outcome.changed,
            provenance={
                "source": "operator",
                "fields": [outcome.field],
                "exceptions": list(outcome.exceptions),
                "decompose": decompose,
            },
        )
        (changes if outcome.changed else skipped).append(change)

    mapping = MappingResult(changes=changes, skipped=skipped)
    plan = build_plan(
        item=item,
        candidate=Candidate(kind=item.kind, name=item.name),
        confidence=operator_confidence(),
        mapping=mapping,
        locked_fields=item.get("LockedFields") or [],
    )
    return plan, outcomes


def _decomposed(
    outcome: ClearOutcome, *, exceptions: tuple[str, ...]
) -> tuple[list[str], list[str]]:
    """Decompose packed values, keeping the components that are exceptions.

    Walks ``outcome.before`` and builds the report from scratch, rather than patching the
    plain clear's answer. The plain clear keeps a value only when the *whole* value is an
    exception, so a packed value whose component is the exception was already dropped --
    decomposing its survivors would find nothing to keep, and patching its ``removed`` list
    would count the value once as a value and again per component.

    Returns the new list and the removed components, named as components so the report says
    what disappeared ("Rock") rather than only which value it came out of ("Rock, Reggae").
    """
    proposed: list[str] = []
    removed: list[str] = []
    for value in outcome.before:
        split = split_kept_component(value, exceptions=exceptions)
        if not split.changed:
            # Nothing to drop from this value, so it stays exactly as it is: a single
            # component that is an exception, or a packed value all of whose components are.
            proposed.append(value)
            continue
        removed.extend(split.removed_components)
        if split.replacement is not None:
            proposed.append(split.replacement)
        # `replacement is None` means no component was an exception, so the whole value goes.
    return proposed, removed


def _reason(outcome: ClearOutcome, *, decompose: bool) -> str:
    how = (
        "and from packed values, keeping only the exceptions" if decompose else "keeping exceptions"
    )
    kept = len(outcome.after)
    return f"clears {outcome.field} {how} ({len(outcome.removed)} value(s) removed, {kept} kept)"


async def find_items(
    client: JellyfinClient,
    *,
    selection: ClearSelection,
) -> SelectionOutcome:
    """The items this clear would act on, and what reading them cost.

    Jellyfin cannot express "has tags to clear" as a query parameter the way it can express
    ``Tags=rock``, so this reads the media type and tests each item locally. Bounded by
    ``MAX_SCAN_ITEMS`` and honest about it (``truncated``), in the same spirit as the
    browse ``missing`` filter and ``genre_removal._scan_for_component``.

    An item that carries nothing in the requested fields is dropped from the selection:
    writing a "clear" to it would be a no-op the report would then have to explain, and the
    exclusion patterns should not be spent on it either.

    Explicit ids skip the paging but not the tests: the caller may have named items that
    carry nothing, or that an exclusion pattern removes.
    """
    if selection.limit > MAX_BATCH_ITEMS:
        raise ValidationError(
            f"limit {selection.limit} exceeds the {MAX_BATCH_ITEMS} item batch cap; "
            "narrow the query or raise the cap deliberately"
        )
    wanted = selection.filter_for()
    item_kind = ITEM_KIND_BY_QUERY.get(selection.kind)
    if item_kind is None:
        raise ValidationError(
            f"unknown selection kind {selection.kind!r}; expected one of "
            f"{sorted(ITEM_KIND_BY_QUERY)}"
        )
    explicit = [item_id for item_id in (selection.ids or []) if item_id]
    if explicit:
        candidates = list(await client.items_by_ids(explicit[: selection.limit]))
        kept = [dto for dto in candidates if wanted.carries(dto) and _has_values(dto, selection)]
        return SelectionOutcome(
            items=kept,
            scanned=len(candidates),
            excluded=len(candidates) - len(kept),
        )

    cap = max(min(MAX_SCAN_ITEMS, max(selection.limit, 1)), 1)
    facets = wanted.jellyfin_params()
    matched: list[Any] = []
    scanned = 0
    excluded = 0
    truncated = True
    while scanned < cap and len(matched) < selection.limit:
        page = await client.items(
            kind=item_kind,
            parent_id=selection.parent_id,
            search_term=selection.search,
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
            if _has_values(dto, selection):
                matched.append(dto)
                if len(matched) >= selection.limit:
                    break
        if len(matched) >= selection.limit:
            # The selection is full: the requested limit was honoured, not a partial read.
            truncated = False
            break
        if scanned >= (page.TotalRecordCount or 0):
            truncated = False
            break
    # The keep list is deliberately absent from this function: it decides *what survives*
    # an item's own lists, not which items are worth looking at. Folding it in here would
    # let an exception hide an item from the batch entirely -- and then an item whose only
    # tag is the kept one would never be reported as "would change nothing".
    return SelectionOutcome(
        items=matched[: selection.limit],
        scanned=scanned,
        excluded=excluded,
        truncated=truncated,
    )


def _has_values(dto: Any, selection: ClearSelection) -> bool:
    """Whether an item carries anything in the fields this clear would write."""
    return any(getattr(dto, name, None) for name in selection.fields)


async def build_job(
    *,
    client: JellyfinClient,
    selection: ClearSelection,
) -> ClearJob:
    """Diff every selected item, recording what clearing would delete. Writes nothing."""
    fields = _fields_for(selection.fields)
    exceptions = _exceptions(selection.exceptions)

    outcome = await find_items(client, selection=selection)
    job = ClearJob(
        job_id=uuid.uuid4().hex,
        batch_id=uuid.uuid4().hex,
        created_at=datetime.now(UTC),
        requested={**selection.as_dict(), "fields": list(fields), "exceptions": list(exceptions)},
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

        plan, outcomes = plan_for_item(
            item, fields=fields, exceptions=exceptions, decompose=selection.decompose
        )
        job.outcomes[item.item_id] = outcomes
        job.items.append(
            BulkItem(
                item_id=item.item_id,
                name=item.name,
                kind=item.kind,
                plan=plan,
                skipped_reason=None if plan.proposed else "carries nothing to clear",
            )
        )

    _clear_registry.add(job)
    log.info("clear_job_built", **job.summary())
    return job


async def stream_job_diff(job: ClearJob) -> AsyncIterator[dict[str, Any]]:
    """Yield the reviewed plan for each item, one event at a time.

    The item frame mirrors ``genre_removal.stream_job_diff`` exactly, including the
    ``removals`` key, so one client renders both. Only the *content* of a removal differs:
    ``target`` is absent and ``exceptions`` is present, which is why the summary event is
    the place the feature's own facts (kept spellings, unmatched exceptions, deleted
    value count) are reported.
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
            "removals": [outcome.as_dict() for outcome in job.outcomes.get(item.item_id, [])],
        }
    yield {
        "type": "summary",
        **job.summary(),
        "emptied": job.emptied,
    }


async def apply_clear_job(
    *,
    session: AsyncSession,
    client: JellyfinClient,
    job: ClearJob,
    selections: dict[str, list[str]] | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """Apply a reviewed clear, isolating failures per item.

    Delegates to ``bulk.apply_job`` so there is exactly one write path, one snapshotted
    write per item, and one batch-revert mechanism for every kind of batch.
    """
    from metaedit.service.bulk import apply_job

    async for event in apply_job(
        session=session,
        client=client,
        job=job,
        selections=selections,
    ):
        yield event


async def get_clear_job_or_404(job_id: str) -> ClearJob:
    job = _clear_registry.get(job_id)
    if job is None:
        raise NotFoundError(
            f"Unknown or expired clear job {job_id!r}. Jobs are held in memory and a "
            "restart clears them; run the diff again."
        )
    return job


__all__ = [
    "DEFAULT_FIELDS",
    "ClearJob",
    "ClearSelection",
    "apply_clear_job",
    "build_job",
    "clear_registry",
    "find_items",
    "get_clear_job_or_404",
    "plan_for_item",
    "stream_job_diff",
]
