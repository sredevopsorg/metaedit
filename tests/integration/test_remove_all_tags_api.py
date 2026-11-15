"""Clearing every tag, with exceptions, against a real database and a stubbed Jellyfin.

The load-bearing tests here are the ones about what must **not** be written: a value on the
keep list must survive the write no matter where it sits, the payload must stay a full
overwrite (ADR 0003) so a clear cannot null a biography, and an exception that matches nothing
must be *reported* rather than silently ignored.

The stub models Jellyfin's genre filter faithfully, but this feature never uses it: "clear
everything except X" is not expressible as a query parameter, so the batch reads items. What
the stub therefore has to get right is paging and the full-overwrite write, both of which it
does because the source of truth for them is the real server's behaviour, not this feature.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from tests.conftest import requires_postgres
from tests.support.jellyfin import apply_facet_filters

from metaedit.adapters.jellyfin.client import JellyfinClient
from metaedit.config import Settings, get_settings
from metaedit.db.session import dispose_engine
from metaedit.main import create_app

pytestmark = requires_postgres

BASE = "http://jellyfin.test:8096"

# Three artists, chosen so every safety property has an example to fail on:
#
# * Alpha carries a tag that is also on the keep list (`Chile`), one that is not (`britpop`),
#   and a `Tags`/`Genres` overlap so "clear Tags only" can be shown not to touch Genres.
# * Beta carries a packed tag value, so "keep Reggae" can be shown *not* to keep
#   "Rock, Reggae" without `decompose`.
# * Gamma carries no tags at all, so it must never appear in the batch.
ARTIST_1: dict[str, Any] = {
    "Id": "aaaaaaaa-0000-0000-0000-000000000001",
    "Type": "MusicArtist",
    "Name": "Alpha",
    "Etag": "etag-1",
    "SourceType": "Library",
    "Genres": ["Ska", "Rock"],
    "Tags": ["britpop", "Chile"],
    "ProviderIds": {"MusicBrainzArtist": "mb-1"},
    "ExternalUrls": [{"Name": "Last.fm", "Url": "https://www.last.fm/music/Alpha"}],
    "Overview": "A biography that must survive a tag clear.",
    "Studios": [],
    "ProductionLocations": [],
    "People": [],
    "LockedFields": [],
    "CommunityRating": 8.5,
    "ProductionYear": 1999,
    "ForcedSortName": "Alpha sort",
    "ArtistItems": [{"Name": "Alpha", "Id": "aaaaaaaa-0000-0000-0000-000000000001"}],
}

ARTIST_2: dict[str, Any] = {
    "Id": "aaaaaaaa-0000-0000-0000-000000000002",
    "Type": "MusicArtist",
    "Name": "Beta",
    "Etag": "etag-2",
    "SourceType": "Library",
    "Genres": ["Jazz"],
    "Tags": ["Rock, Reggae", "ska"],
    "ProviderIds": {},
    "ExternalUrls": [],
    "Overview": "",
    "Studios": [],
    "ProductionLocations": [],
    "People": [],
    "LockedFields": [],
    "ArtistItems": [{"Name": "Beta", "Id": "aaaaaaaa-0000-0000-0000-000000000002"}],
}

ARTIST_3: dict[str, Any] = {
    "Id": "aaaaaaaa-0000-0000-0000-000000000003",
    "Type": "MusicArtist",
    "Name": "Gamma",
    "Etag": "etag-3",
    "SourceType": "Library",
    "Genres": ["Pop"],
    "Tags": [],
    "ProviderIds": {},
    "ExternalUrls": [],
    "Overview": "",
    "Studios": [],
    "ProductionLocations": [],
    "People": [],
    "LockedFields": [],
    "ArtistItems": [{"Name": "Gamma", "Id": "aaaaaaaa-0000-0000-0000-000000000003"}],
}

LIBRARY: dict[str, dict[str, Any]] = {
    ARTIST_1["Id"]: ARTIST_1,
    ARTIST_2["Id"]: ARTIST_2,
    ARTIST_3["Id"]: ARTIST_3,
}
WRITES: list[dict[str, Any]] = []


def _handler(request: httpx.Request) -> httpx.Response:
    """A Jellyfin that pages, filters facets and performs a genuine full-overwrite write."""
    path = request.url.path
    params = request.url.params

    if request.method == "POST" and path.startswith("/Items/"):
        WRITES.append({"item_id": path.rsplit("/", 1)[-1], "body": json.loads(request.content)})
        return httpx.Response(204)

    if path == "/Items":
        raw_ids = params.get("ids")
        if raw_ids:
            found = [LIBRARY[i] for i in raw_ids.split(",") if i in LIBRARY]
            return httpx.Response(200, json={"Items": found, "TotalRecordCount": len(found)})
        found = list(LIBRARY.values())
        wanted_type = params.get("includeItemTypes")
        if wanted_type:
            found = [row for row in found if row.get("Type") == wanted_type]
        found = apply_facet_filters(params, found)
        term = (params.get("searchTerm") or "").casefold()
        if term:
            found = [row for row in found if term in str(row.get("Name", "")).casefold()]
        total = len(found)
        start = int(params.get("startIndex") or 0)
        limit = int(params.get("limit") or 100)
        return httpx.Response(
            200, json={"Items": found[start : start + limit], "TotalRecordCount": total}
        )

    if path.startswith("/Items/"):
        item_id = path.rsplit("/", 1)[-1]
        row = LIBRARY.get(item_id)
        if row is None:
            return httpx.Response(404, json={"error": "no such item"})
        return httpx.Response(200, json=row)

    if path == "/Users":
        return httpx.Response(200, json=[{"Id": "user-1", "Policy": {"IsAdministrator": True}}])
    return httpx.Response(404, json={"error": "unexpected"})


def _settings(database_url: str) -> Settings:
    return Settings(
        _env_file=None,
        JELLYFIN_URL=BASE,
        JELLYFIN_API_KEY="test-admin-key",
        LASTFM_API_KEY="test-lastfm-key",
        LOG_JSON=False,
        DATABASE_URL=database_url,
    )


@pytest.fixture(autouse=True)
def _reset_state() -> Iterator[None]:
    """Clear the stub's write log and all three job registries.

    The registries are module-global and hold jobs in memory by design. Nothing clears them
    between tests, so without this a test that counts jobs sees every job earlier tests made.
    """
    from metaedit.service import bulk, genre_removal, merge_similar, remove_all_tags

    for registry in (
        bulk.registry(),
        genre_removal.removal_registry(),
        remove_all_tags.clear_registry(),
        merge_similar.merge_registry(),
    ):
        registry.clear()
    WRITES.clear()
    yield
    for registry in (
        bulk.registry(),
        genre_removal.removal_registry(),
        remove_all_tags.clear_registry(),
        merge_similar.merge_registry(),
    ):
        registry.clear()
    WRITES.clear()


@pytest.fixture
def client(database_url: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    transport = httpx.MockTransport(_handler)

    async def patched_enter(self: JellyfinClient) -> JellyfinClient:
        self._client = httpx.AsyncClient(transport=transport, timeout=2.0)
        self._owns_client = True
        return self

    monkeypatch.setattr(JellyfinClient, "__aenter__", patched_enter)

    import asyncio

    asyncio.run(dispose_engine())
    settings = _settings(database_url)
    app = create_app(settings)
    app.dependency_overrides[get_settings] = lambda: settings
    try:
        with TestClient(app, raise_server_exceptions=False) as test_client:
            yield test_client
    finally:
        asyncio.run(dispose_engine())


def _events(response: httpx.Response) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    for line in response.text.splitlines():
        if line.startswith("data: "):
            found.append(json.loads(line[6:]))
    return found


def _diff(client: TestClient, payload: dict[str, Any]) -> list[dict[str, Any]]:
    response = client.post("/api/bulk/remove-all-tags/diff", json=payload)
    assert response.status_code == 200, response.text
    return _events(response)


def _summary(events: list[dict[str, Any]]) -> dict[str, Any]:
    summaries = [event for event in events if event["type"] == "summary"]
    assert summaries, events
    return summaries[-1]


def _apply(client: TestClient, job_id: str) -> dict[str, Any]:
    response = client.post(
        "/api/bulk/remove-all-tags/apply", json={"job_id": job_id, "confirm": True}
    )
    assert response.status_code == 200, response.text
    return _summary(_events(response))


def _written(item_id: str) -> dict[str, Any]:
    return next(write["body"] for write in WRITES if write["item_id"] == item_id)


# ------------------------------------------------------------------ selection


def test_the_batch_selects_items_that_carry_tags(client: TestClient) -> None:
    """Gamma carries no tags, so it is not in the batch at all.

    Including it would mean writing a "clear" that removes nothing, which the report would
    then have to explain.
    """
    events = _diff(client, {})
    names = [event["name"] for event in events if event["type"] == "item"]
    assert names == ["Alpha", "Beta"]


def test_the_scan_is_reported(client: TestClient) -> None:
    """Reading is what this selection costs, and it is stated rather than implied."""
    summary = _summary(_diff(client, {}))
    assert summary["scanned"] == 3, "every artist was read to find the two that carry tags"
    assert summary["truncated"] is False


def test_search_narrows_the_selection(client: TestClient) -> None:
    events = _diff(client, {"selection": {"search": "alph"}})
    assert [event["name"] for event in events if event["type"] == "item"] == ["Alpha"]


def test_an_exclusion_pattern_keeps_an_item_out(client: TestClient) -> None:
    events = _diff(client, {"selection": {"exclude": ["alpha"]}})
    names = [event["name"] for event in events if event["type"] == "item"]
    assert names == ["Beta"]

    summary = _summary(events)
    assert summary["excluded"] == 1


def test_an_explicit_id_list_is_still_filtered(client: TestClient) -> None:
    """Naming an item with nothing to clear must not produce a no-op batch entry."""
    events = _diff(client, {"selection": {"ids": [ARTIST_3["Id"]]}})
    assert [event for event in events if event["type"] == "item"] == []


def test_the_limit_is_capped(client: TestClient) -> None:
    response = client.post("/api/bulk/remove-all-tags/diff", json={"selection": {"limit": 5000}})
    assert response.status_code == 422


# ------------------------------------------------------------------- refusals


def test_a_blank_exception_is_refused(client: TestClient) -> None:
    """An empty entry would look like a keep rule and keep nothing."""
    response = client.post(
        "/api/bulk/remove-all-tags/diff", json={"exceptions": ["Chile", "\u0020"]}
    )
    assert response.status_code == 422
    assert "blank" in response.json()["error"]["message"].lower()


def test_an_unknown_field_is_refused(client: TestClient) -> None:
    """A closed field set: this endpoint may not be pointed at ``Overview``."""
    response = client.post("/api/bulk/remove-all-tags/diff", json={"fields": ["Overview"]})
    assert response.status_code == 422


def test_apply_requires_confirm(client: TestClient) -> None:
    job_id = _summary(_diff(client, {}))["job_id"]
    response = client.post("/api/bulk/remove-all-tags/apply", json={"job_id": job_id})
    assert response.status_code == 422
    assert "confirm" in response.json()["error"]["message"]


def test_apply_needs_a_reviewed_job(client: TestClient) -> None:
    response = client.post(
        "/api/bulk/remove-all-tags/apply", json={"job_id": "nope", "confirm": True}
    )
    assert response.status_code == 404


def test_a_removal_job_id_is_not_a_clear_job_id(client: TestClient) -> None:
    """Separate registries, so one kind of job cannot be applied as another."""
    removal = client.post("/api/bulk/remove-genre/diff", json={"genre": "Ska"})
    removal_job = _summary(_events(removal))["job_id"]
    response = client.post(
        "/api/bulk/remove-all-tags/apply", json={"job_id": removal_job, "confirm": True}
    )
    assert response.status_code == 404


# ------------------------------------------------------------------ the diff


def test_the_diff_writes_nothing(client: TestClient) -> None:
    _diff(client, {})
    assert WRITES == []


def test_the_diff_defaults_to_tags_only(client: TestClient) -> None:
    """``Genres`` is what Jellyfin navigates by, so clearing it is opt-in."""
    events = _diff(client, {})
    alpha = next(event for event in events if event["name"] == "Alpha")

    assert [removal["field"] for removal in alpha["removals"]] == ["Tags"]
    assert alpha["removals"][0]["before"] == ["britpop", "Chile"]
    assert alpha["removals"][0]["after"] == []


def test_the_diff_reports_an_exception_as_kept(client: TestClient) -> None:
    events = _diff(client, {"exceptions": ["Chile"]})
    alpha = next(event for event in events if event["name"] == "Alpha")

    [removal] = alpha["removals"]
    assert removal["after"] == ["Chile"], "the exception survives"
    assert removal["removed"] == ["britpop"]
    assert removal["emptied"] is False


def test_the_diff_reports_how_many_items_and_values_it_would_empty(client: TestClient) -> None:
    """The report the whole safety model rests on for an empty keep list.

    "Clears every tag" is the intended use and the largest edit the application offers, so
    the summary states what it would delete rather than making the operator count frames.
    """
    summary = _summary(_diff(client, {}))
    assert summary["clearing"] == ["Tags"]
    assert summary["keeping"] == []
    assert summary["removed_values"] == 4, "Alpha's two tags and Beta's two"
    assert {row["name"] for row in summary["emptied"]} == {"Alpha", "Beta"}


def test_an_exception_that_matches_nothing_is_reported(client: TestClient) -> None:
    """The typo case: silence would look exactly like the exception having worked.

    Matching is exact (ADR 0015), so ``chile `` is not ``Chile`` -- and the operator has to
    be told, because the consequence is that the value they meant to protect is deleted.
    """
    summary = _summary(_diff(client, {"exceptions": ["Chle"]}))
    assert summary["unmatched_exceptions"] == ["Chle"]
    emptied = {row["name"] for row in summary["emptied"]}
    assert emptied == {"Alpha", "Beta"}, "the tag the operator meant to keep is going"


def test_an_exception_that_matches_something_is_not_reported(client: TestClient) -> None:
    summary = _summary(_diff(client, {"exceptions": ["Chile"]}))
    assert summary["unmatched_exceptions"] == []


def test_a_packed_value_is_not_kept_by_one_of_its_components(client: TestClient) -> None:
    """Without ``decompose`` the packed value is one value, and it is not on the keep list."""
    events = _diff(client, {"exceptions": ["Reggae"]})
    beta = next(event for event in events if event["name"] == "Beta")

    [removal] = beta["removals"]
    assert removal["before"] == ["Rock, Reggae", "ska"]
    assert removal["after"] == []
    assert removal["removed"] == ["Rock, Reggae", "ska"]


def test_decompose_keeps_only_the_matching_component(client: TestClient) -> None:
    events = _diff(client, {"exceptions": ["Reggae"], "decompose": True})
    beta = next(event for event in events if event["name"] == "Beta")

    [removal] = beta["removals"]
    assert removal["after"] == ["Reggae"], "the packed value is rebuilt with its own separator"
    assert removal["removed"] == ["Rock", "ska"]


def test_clearing_genres_as_well_is_selectable(client: TestClient) -> None:
    events = _diff(client, {"fields": ["Tags", "Genres"]})
    alpha = next(event for event in events if event["name"] == "Alpha")
    by_field = {removal["field"]: removal for removal in alpha["removals"]}
    assert set(by_field) == {"Genres", "Tags"}
    assert by_field["Genres"]["removed"] == ["Ska", "Rock"]


# ------------------------------------------------------------------- apply


def test_apply_clears_the_tags_and_keeps_the_exception(client: TestClient) -> None:
    """The central behaviour, asserted on the body the server received."""
    summary = _summary(_diff(client, {"exceptions": ["Chile"]}))
    result = _apply(client, summary["job_id"])

    assert result["applied"] == 2, result
    assert sorted(_written(ARTIST_1["Id"])["Tags"]) == ["Chile"], "the exception survived"
    assert _written(ARTIST_2["Id"])["Tags"] == [], "Beta had no exceptions"


def test_apply_never_removes_an_exception(client: TestClient) -> None:
    """Stated separately from the happy path, because it is the failure that matters."""
    summary = _summary(_diff(client, {"exceptions": ["britpop"]}))
    _apply(client, summary["job_id"])

    assert _written(ARTIST_1["Id"])["Tags"] == ["britpop"]


def test_apply_writes_the_whole_item(client: TestClient) -> None:
    """ADR 0003: the payload is a full overwrite, so it must carry every writable field.

    A clear that built its own body would null everything it omitted -- the operator would
    lose the biography and the MusicBrainz id along with the tags.
    """
    summary = _summary(_diff(client, {}))
    _apply(client, summary["job_id"])

    body = _written(ARTIST_1["Id"])
    assert body["Tags"] == []
    assert body["Genres"] == ["Ska", "Rock"], "Genres was not in the requested field set"
    assert body["Overview"] == "A biography that must survive a tag clear."
    assert body["ProviderIds"] == {"MusicBrainzArtist": "mb-1"}
    assert body["ProductionYear"] == 1999
    assert body["CommunityRating"] == 8.5
    assert body["ForcedSortName"] == "Alpha sort"
    assert body["ArtistItems"] == [{"Name": "Alpha", "Id": ARTIST_1["Id"]}]

    from metaedit.domain.writable import payload_field_set

    assert set(body) == payload_field_set("MusicArtist")


def test_apply_snapshots_before_writing(client: TestClient, database_url: str) -> None:
    """ADR 0004: the previous values must be durable before the write."""
    summary = _summary(_diff(client, {"exceptions": ["Chile"]}))
    _apply(client, summary["job_id"])

    import psycopg

    url = database_url.replace("postgresql+psycopg://", "postgresql://")
    with psycopg.connect(url) as conn, conn.cursor() as cursor:
        cursor.execute(
            "select fields, source_op, batch_id from snapshot where item_id = %s",
            (ARTIST_1["Id"],),
        )
        rows = cursor.fetchall()

    assert len(rows) == 1
    fields, source_op, batch_id = rows[0]
    assert source_op == "apply"
    assert batch_id, "the snapshot must carry the batch id, or the batch cannot revert"
    assert fields["Tags"] == ["britpop", "Chile"], "the previous value is what makes it revertible"


def test_the_batch_reverts_through_the_existing_endpoint(
    client: TestClient, database_url: str
) -> None:
    """One revert mechanism for every kind of batch, which is why this shares /bulk."""
    summary = _summary(_diff(client, {}))
    _apply(client, summary["job_id"])

    WRITES.clear()
    response = client.post(f"/api/bulk/{summary['batch_id']}/revert?confirm=true")
    assert response.status_code == 200, response.text

    assert _written(ARTIST_1["Id"])["Tags"] == ["britpop", "Chile"]


def test_a_second_apply_of_the_same_job_is_refused(client: TestClient) -> None:
    job_id = _summary(_diff(client, {}))["job_id"]
    _apply(client, job_id)

    second = client.post(
        "/api/bulk/remove-all-tags/apply", json={"job_id": job_id, "confirm": True}
    )
    assert second.status_code == 200
    assert [event for event in _events(second) if event["type"] == "error"]


def test_the_job_list_reports_what_a_clear_keeps(client: TestClient) -> None:
    _diff(client, {"exceptions": ["Chile"]})
    jobs = client.get("/api/bulk/remove-all-tags/jobs").json()["jobs"]

    assert jobs[-1]["clearing"] == ["Tags"]
    assert jobs[-1]["keeping"] == ["Chile"]
    assert jobs[-1]["unmatched_exceptions"] == []
    assert jobs[-1]["removed_values"] == 3, "Alpha keeps Chile; Beta's two are removed"

    # And the job id is not usable by the other batches' apply endpoints.
    assert client.get("/api/bulk/jobs").json()["count"] == 0
    assert client.get("/api/bulk/remove-genre/jobs").json()["count"] == 0
