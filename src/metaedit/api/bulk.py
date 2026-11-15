"""Bulk editing over Server-Sent Events.

Streaming rather than a single response because the operations are long and partly
irreversible: a diff over 200 items should show progress, and an apply must report each
item as it happens so a failure is visible at the item it happened on rather than at the
end of a batch.

SSE rather than WebSockets because the traffic is one-way progress, and it survives
proxies that would complicate an upgrade -- which matters for a self-hosted tool often
sitting behind a reverse proxy.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Body, Depends, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from metaedit.api.deps import JellyfinDep
from metaedit.api.items import (
    FieldPolicyOverride,
    TagPolicyRequest,
    tag_policy_with_stored_blacklist,
)
from metaedit.api.schemas import (
    BulkJobListResponse,
    BulkStreamEvent,
    ClearJobListResponse,
    GenreVocabularyResponse,
    MergeJobListResponse,
    MergeStreamEvent,
    RemovalJobListResponse,
    RemovalStreamEvent,
    RemoveAllTagsStreamEvent,
    SimilarityPreviewResponse,
)
from metaedit.api.sse import error_frame, sse_frame
from metaedit.config import Settings, get_settings
from metaedit.db.session import get_session
from metaedit.domain.browse_filters import MissingAspect
from metaedit.domain.errors import NotFoundError, ValidationError
from metaedit.service import bulk, genre_removal, merge_similar, remove_all_tags

router = APIRouter(prefix="/bulk", tags=["bulk"])

# A batch is capped well below what the server could return, because the failure mode
# of a too-large batch is not slowness but a library-wide wrong edit.
MAX_ITEMS = bulk.MAX_BATCH_ITEMS

SelectionKind = Literal["artist", "album", "song"]


class BulkSelection(BaseModel):
    """Which items, and nothing else. The fields to write are chosen after review."""

    kind: SelectionKind = "artist"
    ids: list[str] | None = Field(default=None, description="explicit item ids, if known")
    parent_id: str | None = None
    search: str | None = None
    limit: int = Field(default=50, ge=1, le=MAX_ITEMS)
    missing: list[MissingAspect] = Field(
        default_factory=list,
        description=(
            "only items lacking any of these: genres, provider_ids, overview, tags. "
            "Aspects that do not apply to the media type are ignored, so asking a song "
            "selection for `overview` selects nothing rather than everything."
        ),
    )
    artist_ids: list[str] = Field(
        default_factory=list,
        description="only items credited to these artists; applies to `album` and `song`",
    )
    album_ids: list[str] = Field(
        default_factory=list, description="only songs on these albums; applies to `song`"
    )
    exclude: list[str] = Field(
        default_factory=list,
        description=(
            "case-insensitive glob patterns; an item whose name, album or album artist "
            "matches any of them is dropped."
        ),
    )


class BulkDiffRequest(BaseModel):
    selection: BulkSelection = BulkSelection()
    overrides: list[FieldPolicyOverride] = Field(default_factory=list)
    tag_policy: TagPolicyRequest | None = None
    # An item whose best match scores below this is reported as skipped, so a batch
    # cannot quietly include guesses.
    min_confidence: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="skip items whose best candidate scores below this",
    )


class BulkApplyRequest(BaseModel):
    job_id: str = Field(description="from a /bulk/diff run")
    fields: list[str] | None = Field(
        default=None,
        description="fields to write for every item; omit to use each item's default "
        "selection, which is empty for anything needing review",
    )
    selections: dict[str, list[str]] | None = Field(
        default=None,
        description="per-item fields, keyed by item id. This is what review produces: an "
        "album and an artist do not share a field set, so one list for both would either "
        "write a field an item should not change or skip one it should. An item absent "
        "from the map writes nothing.",
    )
    confirm: bool = Field(default=False, description="must be true")


async def _stream(events: AsyncIterator[dict[str, Any]]) -> AsyncIterator[str]:
    """Wrap a service generator as SSE, ending with a terminating frame.

    Failures are reported in-band as an ``error`` event rather than by breaking the
    stream, so a client that has already received per-item results keeps them. The
    frame is built by ``api.sse``, which keeps internal detail out of it.
    """
    try:
        async for event in events:
            yield sse_frame(event)
    except Exception as exc:
        # `error_frame` is what decides whether this exception's text is safe to send;
        # a `MetaeditError` keeps its own code and message, anything else is generic.
        yield sse_frame(error_frame(exc, code="internal_error"))
    yield sse_frame({"type": "done"})


@router.post(
    "/diff",
    responses={
        200: {
            "model": BulkStreamEvent,
            "description": "Server-Sent Events, one frame per item.",
            "content": {"text/event-stream": {}},
        }
    },
)
async def bulk_diff(
    client: JellyfinDep,
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    body: Annotated[BulkDiffRequest, Body()] = BulkDiffRequest(),
) -> StreamingResponse:
    """Diff every item in the selection, streaming one event per item.

    **Writes nothing.** The job id in the summary event is what a later ``/bulk/apply``
    must present, so an apply can never be the first thing that happens.
    """
    job = await bulk.build_job(
        session=session,
        client=client,
        kind=body.selection.kind,
        parent_id=body.selection.parent_id,
        search=body.selection.search,
        ids=body.selection.ids,
        limit=body.selection.limit,
        missing=body.selection.missing,
        artist_ids=body.selection.artist_ids,
        album_ids=body.selection.album_ids,
        exclude=body.selection.exclude,
        overrides={override.field: override.mode for override in body.overrides},
        tag_policy=await tag_policy_with_stored_blacklist(session, settings, body.tag_policy),
        min_confidence=body.min_confidence,
    )
    return StreamingResponse(
        _stream(bulk.stream_job_diff(job)),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


@router.post(
    "/apply",
    responses={
        200: {
            "model": BulkStreamEvent,
            "description": "Server-Sent Events, one frame per item.",
            "content": {"text/event-stream": {}},
        }
    },
)
async def bulk_apply(
    client: JellyfinDep,
    session: Annotated[AsyncSession, Depends(get_session)],
    body: Annotated[BulkApplyRequest, Body()],
) -> StreamingResponse:
    """Apply a reviewed job, streaming progress and isolating failures per item.

    ``confirm`` must be true. The job must exist, which means a diff was reviewed in
    this process: a bulk write is never the first request of a session.
    """
    if not body.confirm:
        raise ValidationError(
            "Refusing to apply without confirm=true. Run /bulk/diff, review the result "
            "and its job_id, then confirm."
        )
    job = bulk.registry().get(body.job_id)
    if job is None:
        raise NotFoundError(
            f"Unknown or expired diff job {body.job_id!r}. Diff jobs are held in memory "
            "and a restart clears them; run /bulk/diff again."
        )

    return StreamingResponse(
        _stream(
            bulk.apply_job(
                session=session,
                client=client,
                job=job,
                fields=body.fields,
                selections=body.selections,
            )
        ),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


@router.post(
    "/{batch_id}/revert",
    responses={
        200: {
            "model": BulkStreamEvent,
            "description": "Server-Sent Events, one frame per item.",
            "content": {"text/event-stream": {}},
        }
    },
)
async def bulk_revert(
    batch_id: str,
    client: JellyfinDep,
    session: Annotated[AsyncSession, Depends(get_session)],
    confirm: bool = False,
) -> StreamingResponse:
    """Undo every item written by a batch, oldest first, isolating failures.

    The batch id lives on the snapshots, so this works after a restart even though the
    diff job that produced it does not.
    """
    if not confirm:
        raise ValidationError("Refusing to revert a batch without confirm=true.")
    return StreamingResponse(
        _stream(bulk.revert_batch(session=session, client=client, batch_id=batch_id)),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


@router.get("/jobs", response_model=BulkJobListResponse)
async def list_jobs() -> dict[str, Any]:
    """Reviewed diffs still available to apply, newest last.

    Exposed because "which batch am I about to apply" should be answerable before an
    apply, not discovered afterwards.
    """
    jobs = bulk.registry().list()
    return {"jobs": [job.summary() for job in jobs], "count": len(jobs)}


# --------------------------------------------------------------- genre removal
#
# A second kind of batch that shares this module's write path. Kept under /bulk because
# that is what it is -- a reviewed, snapshot-backed, revertible batch -- and because
# `/{batch_id}/revert` already serves it, so a removal run is undoable by the same call
# as any other batch.


def _default_removal_fields() -> list[Literal["Genres", "Tags"]]:
    """`Genres` by default.

    A named function rather than a lambda because pydantic's `default_factory` is typed
    against the declared element type, and a lambda returning `list[str]` does not satisfy
    `list[Literal["Genres", "Tags"]]` -- the annotation here is what makes it fit.
    """
    return ["Genres"]


class GenreRemovalSelection(BaseModel):
    """Which items to look in.

    No `missing` filter here, unlike `BulkSelection`: this tool selects by *having* a
    genre, which Jellyfin can express natively and cheaply, rather than by lacking
    something, which it cannot.
    """

    kind: SelectionKind = "artist"
    ids: list[str] | None = Field(default=None, description="explicit item ids, if known")
    parent_id: str | None = None
    search: str | None = None
    limit: int = Field(default=100, ge=1, le=MAX_ITEMS)
    artist_ids: list[str] = Field(
        default_factory=list,
        description="only items credited to these artists; applies to `album` and `song`",
    )
    album_ids: list[str] = Field(
        default_factory=list, description="only songs on these albums; applies to `song`"
    )
    exclude: list[str] = Field(
        default_factory=list,
        description=(
            "case-insensitive glob patterns; an item whose name, album or album artist "
            "matches any of them is dropped. Worth more here than anywhere else: "
            "removing a genre from every compilation is rarely what is meant."
        ),
    )


class GenreRemovalRequest(BaseModel):
    """The value to remove, and where."""

    selection: GenreRemovalSelection = GenreRemovalSelection()
    genre: str = Field(
        description="the genre value to remove. Matched exactly and case-insensitively; "
        "a blank value is refused rather than treated as a wildcard.",
    )
    fields: list[Literal["Genres", "Tags"]] = Field(
        default_factory=_default_removal_fields,
        description="which arrays to remove from. Both are written by this application.",
    )
    decompose: bool = Field(
        default=False,
        description="also remove matching parts of a packed value ('Rock, Reggae' with "
        "genre 'Reggae' becomes 'Rock'). Off by default: it is the destructive reading of "
        "a value that may be one genre whose name contains a separator.",
    )


class GenreRemovalApplyRequest(BaseModel):
    job_id: str = Field(description="from a /bulk/remove-genre/diff run")
    selections: dict[str, list[str]] | None = Field(
        default=None,
        description="per-item fields, keyed by item id. An item absent from the map writes "
        "nothing, so an unreviewed item is a no-op.",
    )
    confirm: bool = Field(default=False, description="must be true")


def _removal_selection(
    body: GenreRemovalSelection,
    *,
    genre: str,
    fields: Sequence[str],
    decompose: bool,
) -> genre_removal.RemovalSelection:
    """Translate the request body into the service's selection value."""
    return genre_removal.RemovalSelection(
        kind=body.kind,
        parent_id=body.parent_id,
        search=body.search,
        ids=body.ids,
        limit=body.limit,
        target=genre,
        fields=tuple(fields or ("Genres",)),
        decompose=decompose,
        artist_ids=tuple(body.artist_ids),
        album_ids=tuple(body.album_ids),
        exclude=tuple(body.exclude),
    )


@router.post(
    "/remove-genre/diff",
    responses={
        200: {
            "model": RemovalStreamEvent,
            "description": "Server-Sent Events, one frame per item.",
            "content": {"text/event-stream": {}},
        }
    },
)
async def remove_genre_diff(
    client: JellyfinDep,
    body: Annotated[GenreRemovalRequest, Body()],
) -> StreamingResponse:
    """Find every item carrying a genre, and show what removing it would do.

    **Writes nothing.** The job id in the summary is what a later apply must present, so a
    library-wide removal can never be the first thing a session does.

    Selection uses Jellyfin's own ``Genres``/``Tags`` filter, which is an exact,
    case-insensitive, server-side match -- verified live. So the reported count is real
    and there is no scan to truncate.
    """
    job = await genre_removal.build_job(
        client=client,
        selection=_removal_selection(
            body.selection,
            genre=body.genre,
            fields=body.fields,
            decompose=body.decompose,
        ),
    )
    return StreamingResponse(
        _stream(genre_removal.stream_job_diff(job)),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


@router.post(
    "/remove-genre/apply",
    responses={
        200: {
            "model": RemovalStreamEvent,
            "description": "Server-Sent Events, one frame per item.",
            "content": {"text/event-stream": {}},
        }
    },
)
async def remove_genre_apply(
    client: JellyfinDep,
    session: Annotated[AsyncSession, Depends(get_session)],
    body: Annotated[GenreRemovalApplyRequest, Body()],
) -> StreamingResponse:
    """Apply a reviewed removal, streaming progress and isolating failures per item.

    ``confirm`` must be true and the job must exist, so a removal is never the first
    request of a session. Every write is snapshotted and shares the job's ``batch_id``,
    so the whole run is undoable through ``/{batch_id}/revert``.
    """
    if not body.confirm:
        raise ValidationError(
            "Refusing to remove a genre without confirm=true. Run /bulk/remove-genre/diff, "
            "review the result and its job_id, then confirm."
        )
    job = await genre_removal.get_removal_job_or_404(body.job_id)

    return StreamingResponse(
        _stream(
            genre_removal.apply_removal_job(
                session=session, client=client, job=job, selections=body.selections
            )
        ),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


@router.get("/remove-genre/jobs", response_model=RemovalJobListResponse)
async def list_removal_jobs() -> dict[str, Any]:
    """Reviewed removals still available to apply."""
    jobs = genre_removal.removal_registry().list()
    return {"jobs": [job.summary() for job in jobs], "count": len(jobs)}


@router.get("/genres", response_model=GenreVocabularyResponse)
async def library_genres(
    client: JellyfinDep,
    item_kind: Annotated[
        Literal["MusicArtist", "MusicAlbum", "Audio"],
        Query(description="which media type's genre set to list; the sets differ"),
    ] = "MusicArtist",
) -> dict[str, Any]:
    """The genre vocabulary this library actually uses.

    Read from Jellyfin's genre entities rather than by scanning items: it is the same list
    the server's own genre filter offers (39 entries for this library's artists), so what
    the operator picks here is what they would pick there. Read-only.

    Scoped per media type because the sets genuinely differ -- an album-only genre is not
    an artist genre, and listing one type's genres for another would offer a value that
    matches nothing.
    """
    entities = await client.genres(item_kind=item_kind)
    names = sorted({entity.Name for entity in entities if entity.Name})
    return {
        "genres": names,
        "count": len(names),
        "note": (
            "The library's genre entities, which is what Jellyfin's own filter lists. "
            "Removal matches a value exactly and case-insensitively."
        ),
    }


# ------------------------------------------------------- clearing every tag
#
# The same reviewed lifecycle under /bulk as a genre removal, because that is what it is:
# a snapshot-backed, revertible batch. `/{batch_id}/revert` already serves it, so a run that
# clears a library's tags is undone by the same call as any other batch.
#
# The one thing it cannot borrow is *selection*. A genre removal asks Jellyfin for "items
# carrying genre X", which it can filter exactly; "clear everything except X" is a question
# no query parameter expresses, so the batch reads items and says how many it read and
# whether the read stopped at the cap.


def _default_clear_fields() -> list[Literal["Genres", "Tags"]]:
    """`Tags` by default.

    A named function rather than a lambda for the same reason `_default_removal_fields` is
    one: pydantic types `default_factory` against the declared element type.

    `Tags` and not both arrays. `Genres` is what Jellyfin navigates and filters by, so
    clearing it is a much larger edit than the request ("remove all tags") describes; it is
    available through `fields` for an operator who means it.
    """
    return ["Tags"]


class ClearSelection(BaseModel):
    """Which items to clear, and the same narrowings every batch accepts.

    No `missing` filter here, unlike `BulkSelection`: an item with no tags has nothing to
    clear, so asking for it would select a batch that writes nothing.
    """

    kind: SelectionKind = "artist"
    ids: list[str] | None = Field(default=None, description="explicit item ids, if known")
    parent_id: str | None = None
    search: str | None = None
    limit: int = Field(default=100, ge=1, le=MAX_ITEMS)
    artist_ids: list[str] = Field(
        default_factory=list,
        description="only items credited to these artists; applies to `album` and `song`",
    )
    album_ids: list[str] = Field(
        default_factory=list, description="only songs on these albums; applies to `song`"
    )
    exclude: list[str] = Field(
        default_factory=list,
        description=(
            "case-insensitive glob patterns; an item whose name, album or album artist "
            "matches any of them is dropped. Worth more here than anywhere else: clearing "
            "every tag from the compilations is rarely meant."
        ),
    )


class RemoveAllTagsRequest(BaseModel):
    """Which fields to clear, and what to keep."""

    selection: ClearSelection = ClearSelection()
    fields: list[Literal["Genres", "Tags"]] = Field(
        default_factory=_default_clear_fields,
        description="which arrays to clear. `Tags` by default; `Genres` is opt-in because "
        "it is what Jellyfin navigates and filters by.",
    )
    exceptions: list[str] = Field(
        default_factory=list,
        description=(
            "values to KEEP. Matched exactly and case-insensitively, never as a substring: "
            "keeping 'Reggae' does not keep 'Rock, Reggae'. An empty list clears everything, "
            "which is the intended use and the largest edit this application offers -- the "
            "diff reports how many items and values it would empty."
        ),
    )
    decompose: bool = Field(
        default=False,
        description="also apply inside a packed value ('Rock, Reggae' with exceptions "
        "['Reggae'] becomes 'Reggae'). Off by default: it is the destructive reading of a "
        "value that may be one genre whose name contains a separator.",
    )


class ClearApplyRequest(BaseModel):
    job_id: str = Field(description="from a /bulk/remove-all-tags/diff run")
    selections: dict[str, list[str]] | None = Field(
        default=None,
        description="per-item fields, keyed by item id. An item absent from the map falls "
        "back to its own default, which for a clear is the field set the operator asked "
        "about.",
    )
    confirm: bool = Field(default=False, description="must be true")


def _clear_selection(
    body: ClearSelection,
    *,
    fields: Sequence[str],
    exceptions: Sequence[str],
    decompose: bool,
) -> remove_all_tags.ClearSelection:
    """Translate the request body into the service's selection value."""
    return remove_all_tags.ClearSelection(
        kind=body.kind,
        parent_id=body.parent_id,
        search=body.search,
        ids=body.ids,
        limit=body.limit,
        fields=tuple(fields or ("Tags",)),
        exceptions=tuple(exceptions),
        decompose=decompose,
        artist_ids=tuple(body.artist_ids),
        album_ids=tuple(body.album_ids),
        exclude=tuple(body.exclude),
    )


@router.post(
    "/remove-all-tags/diff",
    responses={
        200: {
            "model": RemoveAllTagsStreamEvent,
            "description": "Server-Sent Events, one frame per item.",
            "content": {"text/event-stream": {}},
        }
    },
)
async def remove_all_tags_diff(
    client: JellyfinDep,
    body: Annotated[RemoveAllTagsRequest, Body()],
) -> StreamingResponse:
    """Show what clearing every tag (except the exceptions) would do.

    **Writes nothing.** The job id in the summary is what a later apply must present, so a
    library-wide clear can never be the first thing a session does.

    The summary reports what the operator has to see before confirming: how many items and
    how many stored values would be deleted, which fields would be left empty, and which
    exceptions matched nothing. That last one is the difference between a keep list and a
    typo -- matching is exact, so a misspelt exception keeps nothing at all.

    Selection is *read*: Jellyfin can filter by one genre but cannot express "everything
    except X", so the batch pages through the media type, bounded by the scan cap. The
    summary says how many items were read and whether the read stopped at the cap.
    """
    job = await remove_all_tags.build_job(
        client=client,
        selection=_clear_selection(
            body.selection,
            fields=body.fields,
            exceptions=body.exceptions,
            decompose=body.decompose,
        ),
    )
    return StreamingResponse(
        _stream(remove_all_tags.stream_job_diff(job)),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


@router.post(
    "/remove-all-tags/apply",
    responses={
        200: {
            "model": RemoveAllTagsStreamEvent,
            "description": "Server-Sent Events, one frame per item.",
            "content": {"text/event-stream": {}},
        }
    },
)
async def remove_all_tags_apply(
    client: JellyfinDep,
    session: Annotated[AsyncSession, Depends(get_session)],
    body: Annotated[ClearApplyRequest, Body()],
) -> StreamingResponse:
    """Apply a reviewed clear, streaming progress and isolating failures per item.

    ``confirm`` must be true and the job must exist, so a clear is never the first request
    of a session. Every write is snapshotted before it happens and shares the job's
    ``batch_id``, so the whole run is undoable through ``/{batch_id}/revert``.
    """
    if not body.confirm:
        raise ValidationError(
            "Refusing to clear tags without confirm=true. Run /bulk/remove-all-tags/diff, "
            "review what it would delete and what it would keep, then confirm with its "
            "job_id."
        )
    job = await remove_all_tags.get_clear_job_or_404(body.job_id)

    return StreamingResponse(
        _stream(
            remove_all_tags.apply_clear_job(
                session=session, client=client, job=job, selections=body.selections
            )
        ),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


@router.get("/remove-all-tags/jobs", response_model=ClearJobListResponse)
async def list_clear_jobs() -> dict[str, Any]:
    """Reviewed clears still available to apply, with what they keep."""
    jobs = remove_all_tags.clear_registry().list()
    return {"jobs": [job.summary() for job in jobs], "count": len(jobs)}


# --------------------------------------------------- merging similar spellings
#
# Two endpoints and a review step between them, because the decision this feature makes --
# "these two values are the same genre" -- is a judgement, and a judgement applied
# automatically is a library-wide rename nobody asked for. `preview` never writes; `diff`
# requires the groups it was shown; `apply` requires the job id from `diff`. The normal
# Last.fm diff is untouched: ADR 0015's exact matching still governs what it proposes.


def _default_preview_fields() -> list[Literal["Genres", "Tags"]]:
    """Both arrays: a preview is a reader, so seeing less of the library is a worse answer.

    A named function rather than a lambda for the same annotation reason as the two
    ``_default_*_fields`` helpers above.
    """
    return ["Genres", "Tags"]


class SimilarityPreviewRequest(BaseModel):
    """Where the vocabulary comes from, and what to hold back."""

    kind: Literal["artist", "album", "song"] = Field(
        default="artist",
        description="media type to read when `values` is omitted",
    )
    parent_id: str | None = Field(default=None, description="a Jellyfin library id to read")
    fields: list[Literal["Genres", "Tags"]] = Field(
        default_factory=_default_preview_fields,
        description="which arrays to compare. Both by default: they hold different "
        "vocabularies, and only one of them being listed would hide half the spellings.",
    )
    values: list[str] | None = Field(
        default=None,
        description="compare this explicit list instead of reading the library",
    )
    keep: list[str] = Field(
        default_factory=list,
        description="genres whose whole group must be left alone",
    )
    exceptions: list[str] = Field(
        default_factory=list,
        description="individual spellings never to merge. An exception that matches "
        "nothing is reported in `hints` rather than ignored.",
    )


@router.post("/merge-similar/preview", response_model=SimilarityPreviewResponse)
async def merge_similar_preview(
    client: JellyfinDep,
    body: Annotated[SimilarityPreviewRequest, Body()] = SimilarityPreviewRequest(),
) -> dict[str, Any]:
    """Group values that are the same genre spelled differently. **Writes nothing.**

    ``Hip Hop``, ``hip-hop`` and ``HipHop`` are one genre written three ways; this reports
    the groups, how often each spelling occurs, and the canonical spelling a merge would
    keep. It does not merge anything, and nothing consumes it automatically: the operator
    confirms groups by name at ``/merge-similar/diff``.

    Reading the library is bounded by the scan cap, and ``truncated`` says so: a preview
    that saw part of a library saw part of its vocabulary. Read-only throughout.
    """
    fields = body.fields or ["Genres", "Tags"]
    if body.values is not None:
        # One explicit list, so one field. Naming two would be ambiguous about which array
        # a group describes, and the frame has to say.
        if len(fields) != 1:
            raise ValidationError(
                "an explicit `values` list belongs to one field; name exactly one of "
                "`Genres` or `Tags` in `fields`"
            )
        vocabulary = merge_similar.vocabulary_from_lists(body.values, field=fields[0])
    else:
        vocabulary = await merge_similar.read_vocabulary(
            client, kind=body.kind, parent_id=body.parent_id, fields=fields
        )
    result = merge_similar.preview(vocabulary, keep=body.keep, exceptions=body.exceptions)
    return result.as_dict()


class MergeGroupRequest(BaseModel):
    """One group the operator confirmed, exactly as the preview reported it."""

    field: Literal["Genres", "Tags"]
    representative: str
    spellings: list[str]


class MergeSimilarRequest(BaseModel):
    """Confirmed groups, and where to apply them."""

    selection: ClearSelection = ClearSelection()
    groups: list[MergeGroupRequest] = Field(
        description="the groups to merge. Required: a merge never runs automatically, and "
        "a group the similarity rule would not have produced is refused.",
    )


@router.post(
    "/merge-similar/diff",
    responses={
        200: {
            "model": MergeStreamEvent,
            "description": "Server-Sent Events, one frame per item.",
            "content": {"text/event-stream": {}},
        }
    },
)
async def merge_similar_diff(
    client: JellyfinDep,
    body: Annotated[MergeSimilarRequest, Body()],
) -> StreamingResponse:
    """Diff a confirmed merge over a selection. **Writes nothing.**

    The groups in the body are what makes this a review rather than an automatic pass: they
    come from ``/merge-similar/preview``, and every one is re-checked against the similarity
    rule before anything is planned. An item that carries a spelling the preview did not
    group is reported as skipped rather than half-merged.

    Selection is read rather than forwarded: Jellyfin cannot ask for "any of these spellings,
    in either of two arrays", so the batch pages the media type and says what it read.
    """
    job = await merge_similar.build_job(
        client=client,
        merge_selection=merge_similar.MergeSelection(
            kind=body.selection.kind,
            parent_id=body.selection.parent_id,
            search=body.selection.search,
            ids=body.selection.ids,
            limit=body.selection.limit,
            artist_ids=tuple(body.selection.artist_ids),
            album_ids=tuple(body.selection.album_ids),
            exclude=tuple(body.selection.exclude),
        ),
        groups=[group.model_dump() for group in body.groups],
    )
    return StreamingResponse(
        _stream(merge_similar.stream_job_diff(job)),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


@router.post(
    "/merge-similar/apply",
    responses={
        200: {
            "model": MergeStreamEvent,
            "description": "Server-Sent Events, one frame per item.",
            "content": {"text/event-stream": {}},
        }
    },
)
async def merge_similar_apply(
    client: JellyfinDep,
    session: Annotated[AsyncSession, Depends(get_session)],
    body: Annotated[ClearApplyRequest, Body()],
) -> StreamingResponse:
    """Apply a reviewed merge, streaming progress and isolating failures per item.

    ``confirm`` must be true and the job must exist, so a merge is never the first request
    of a session and never happens without a preview of the exact groups it applies. Every
    write is snapshotted before it happens and shares the job's ``batch_id``, so the run is
    undoable through ``/{batch_id}/revert``.
    """
    if not body.confirm:
        raise ValidationError(
            "Refusing to merge without confirm=true. Run /bulk/merge-similar/preview, "
            "review the groups, then /bulk/merge-similar/diff and confirm its job_id."
        )
    job = await merge_similar.get_merge_job_or_404(body.job_id)

    return StreamingResponse(
        _stream(
            merge_similar.apply_merge_job(
                session=session, client=client, job=job, selections=body.selections
            )
        ),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


@router.get("/merge-similar/jobs", response_model=MergeJobListResponse)
async def list_merge_jobs() -> dict[str, Any]:
    """Reviewed merges still available to apply, with the groups they apply."""
    jobs = merge_similar.merge_registry().list()
    return {"jobs": [job.summary() for job in jobs], "count": len(jobs)}
