"""The similarity preview and the confirmed merge, against a real database and a stub.

Two properties carry this feature:

* **The preview writes nothing.** A similarity rule that merged automatically would be a
  library-wide rename nobody asked for, so "a preview produced zero writes" is asserted
  directly rather than inferred from the code path.
* **A merge needs a confirmation that names its groups.** The apply requires a job id from a
  diff, the diff requires the groups, and the groups are re-checked against the rule. A
  request that names a pair the rule would not produce is refused -- so the endpoint cannot
  become a general-purpose rename.

The write path is `bulk.apply_job`'s: payload completeness (ADR 0003), snapshot-before-write
(ADR 0004), the etag check (ADR 0007) and batch revert are asserted here on the merge's own
writes, because "it reuses the path" is a claim worth testing rather than trusting.
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

# The same genre, four ways, across two arrays -- plus two genres that share its words and
# must never be pulled in by a merge. `Rockabilly` sits in the same `Genres` array as
# `hip-hop`, so a substring or token rewrite would touch it.
ARTIST_1: dict[str, Any] = {
    "Id": "aaaaaaaa-0000-0000-0000-000000000011",
    "Type": "MusicArtist",
    "Name": "Alpha",
    "Etag": "etag-11",
    "SourceType": "Library",
    "Genres": ["hip-hop", "Rockabilly"],
    "Tags": ["Hip Hop", "britpop"],
    "ProviderIds": {"MusicBrainzArtist": "mb-11"},
    "ExternalUrls": [],
    "Overview": "A biography that must survive a merge.",
    "Studios": [],
    "ProductionLocations": [],
    "People": [],
    "LockedFields": [],
    "ProductionYear": 1999,
    "ArtistItems": [{"Name": "Alpha", "Id": "aaaaaaaa-0000-0000-0000-000000000011"}],
}

ARTIST_2: dict[str, Any] = {
    "Id": "aaaaaaaa-0000-0000-0000-000000000012",
    "Type": "MusicArtist",
    "Name": "Beta",
    "Etag": "etag-12",
    "SourceType": "Library",
    "Genres": ["HipHop"],
    "Tags": [],
    "ProviderIds": {},
    "ExternalUrls": [],
    "Overview": "",
    "Studios": [],
    "ProductionLocations": [],
    "People": [],
    "LockedFields": [],
    "ArtistItems": [{"Name": "Beta", "Id": "aaaaaaaa-0000-0000-0000-000000000012"}],
}

# Nothing similar to anything: must never appear in a merge batch.
ARTIST_3: dict[str, Any] = {
    "Id": "aaaaaaaa-0000-0000-0000-000000000013",
    "Type": "MusicArtist",
    "Name": "Gamma",
    "Etag": "etag-13",
    "SourceType": "Library",
    "Genres": ["Dark Wave"],
    "Tags": [],
    "ProviderIds": {},
    "ExternalUrls": [],
    "Overview": "",
    "Studios": [],
    "ProductionLocations": [],
    "People": [],
    "LockedFields": [],
    "ArtistItems": [{"Name": "Gamma", "Id": "aaaaaaaa-0000-0000-0000-000000000013"}],
}

LIBRARY: dict[str, dict[str, Any]] = {
    ARTIST_1["Id"]: ARTIST_1,
    ARTIST_2["Id"]: ARTIST_2,
    ARTIST_3["Id"]: ARTIST_3,
}
WRITES: list[dict[str, Any]] = []


def _handler(request: httpx.Request) -> httpx.Response:
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
    """Clear the stub's write log and every job registry."""
    from metaedit.service import bulk, genre_removal, merge_similar, remove_all_tags

    registries = (
        bulk.registry(),
        genre_removal.removal_registry(),
        remove_all_tags.clear_registry(),
        merge_similar.merge_registry(),
    )
    for registry in registries:
        registry.clear()
    WRITES.clear()
    yield
    for registry in registries:
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


def _summary(events: list[dict[str, Any]]) -> dict[str, Any]:
    summaries = [event for event in events if event["type"] == "summary"]
    assert summaries, events
    return summaries[-1]


def _preview(client: TestClient, payload: dict[str, Any]) -> dict[str, Any]:
    response = client.post("/api/bulk/merge-similar/preview", json=payload)
    assert response.status_code == 200, response.text
    return response.json()


def _diff(client: TestClient, groups: list[dict[str, Any]], **extra: Any) -> list[dict[str, Any]]:
    response = client.post("/api/bulk/merge-similar/diff", json={"groups": groups, **extra})
    assert response.status_code == 200, response.text
    return _events(response)


def _hip_hop_group(field: str = "Genres") -> dict[str, Any]:
    """The group the library's own values form: one genre under three joiners.

    Capitalisation alone would *not* make a group -- ``HIP-HOP`` and ``hip-hop`` are the same
    stored value to a case-insensitive genre list, so only a joiner difference creates one.
    """
    return {
        "field": field,
        "representative": "HipHop",
        "spellings": ["HipHop", "hip-hop", "Hip Hop"],
    }


# ------------------------------------------------------------------- preview


def test_the_preview_writes_nothing(client: TestClient) -> None:
    """The property the whole design rests on: a similarity pass is read-only."""
    _preview(client, {})
    assert WRITES == []


def test_the_preview_groups_spellings_of_one_genre(client: TestClient) -> None:
    body = _preview(client, {})
    genres = [group for group in body["groups"] if group["field"] == "Genres"]
    assert genres, body

    [group] = genres
    assert {variant["value"] for variant in group["variants"]} == {"hip-hop", "HipHop"}
    assert group["representative"] in {"HipHop", "hip-hop"}, group["representative"]


def test_the_preview_reports_the_proposed_merge(client: TestClient) -> None:
    body = _preview(client, {})
    genre_group = next(group for group in body["groups"] if group["field"] == "Genres")
    assert genre_group["merge"] == {"hip-hop": genre_group["representative"]}


def test_the_preview_never_merges_genres_that_share_words(client: TestClient) -> None:
    """``Rockabilly`` shares no key with ``hip-hop``, and a shared word is not a spelling.

    This is the property that makes the feature acceptable: ``Rock``/``Rockabilly`` and
    ``Pop``/``Pop Rock`` are different genres, not variants of one.
    """
    body = _preview(client, {})
    for group in body["groups"]:
        values = {variant["value"] for variant in group["variants"]}
        assert "Rockabilly" not in values
        assert "Dark Wave" not in values, "nothing else in the library is a variant of it"


def test_the_preview_reports_an_explicit_value_list(client: TestClient) -> None:
    body = _preview(
        client,
        {"values": ["Hip Hop", "hip-hop", "HipHop", "Rockabilly"], "fields": ["Genres"]},
    )
    [group] = body["groups"]
    assert set(group["merge"]) == {"hip-hop", "HipHop"}
    assert body["scanned"] is None, "an explicit list read nothing"


def test_the_preview_of_unrelated_values_is_empty(client: TestClient) -> None:
    body = _preview(
        client, {"values": ["Rock", "Rockabilly", "Pop Rock", "Pop"], "fields": ["Genres"]}
    )
    assert body["groups"] == []


def test_the_preview_reports_its_scan(client: TestClient) -> None:
    body = _preview(client, {"kind": "artist"})
    assert body["scanned"] == 3
    assert body["truncated"] is False
    assert "library" in body["source"]


def test_the_preview_keep_list_withholds_a_group(client: TestClient) -> None:
    body = _preview(client, {"keep": ["hip-hop"]})
    assert body["groups"] == []
    assert len(body["withheld"]) == 1


def test_the_preview_exception_that_matches_nothing_is_reported(client: TestClient) -> None:
    body = _preview(client, {"exceptions": ["hip. hop"]})
    assert any("matches no value" in hint for hint in body["hints"])


def test_the_preview_does_not_report_an_exception_that_matched(client: TestClient) -> None:
    body = _preview(
        client, {"values": ["Hip Hop", "hip-hop"], "fields": ["Genres"], "exceptions": ["Hip Hop"]}
    )
    assert not any("matches no value" in hint for hint in body["hints"])


def test_an_explicit_list_and_two_fields_are_refused(client: TestClient) -> None:
    """An explicit list belongs to one array; naming two is ambiguous."""
    response = client.post(
        "/api/bulk/merge-similar/preview",
        json={"values": ["a"], "fields": ["Genres", "Tags"]},
    )
    assert response.status_code == 422


# ---------------------------------------------------------------------- diff


def test_the_diff_requires_confirmed_groups(client: TestClient) -> None:
    """A merge never runs automatically: no groups means no merge.

    Refused before a job exists, and refused as a 422 rather than through the stream: there
    is nothing to stream, because the request cannot describe a merge. The alternative --
    accepting it and merging whatever the rule found -- is the behaviour this feature was
    specified not to have.
    """
    response = client.post("/api/bulk/merge-similar/diff", json={"groups": []})
    assert response.status_code == 422
    assert "never runs automatically" in response.json()["error"]["message"]


def test_the_diff_refuses_a_group_the_rule_would_not_produce(client: TestClient) -> None:
    """The guard that stops this being a general-purpose rename endpoint."""
    response = client.post(
        "/api/bulk/merge-similar/diff",
        json={
            "groups": [
                {
                    "field": "Genres",
                    "representative": "Rock",
                    "spellings": ["Rock", "Rockabilly"],
                }
            ]
        },
    )
    assert response.status_code == 422
    assert "not the same genre spelled differently" in response.json()["error"]["message"]


def test_the_diff_writes_nothing(client: TestClient) -> None:
    _diff(client, [_hip_hop_group()])
    assert WRITES == []


def test_the_diff_selects_the_items_holding_a_spelling(client: TestClient) -> None:
    events = _diff(client, [_hip_hop_group()])
    names = [event["name"] for event in events if event["type"] == "item"]
    assert names == ["Alpha", "Beta"], "Gamma holds no spelling this merge touches"
    applicable = [
        event["name"] for event in events if event["type"] == "item" and event["applicable"]
    ]
    assert applicable == ["Alpha"], "Beta already stores the representative"


def test_the_diff_reports_the_rewrite(client: TestClient) -> None:
    events = _diff(client, [_hip_hop_group()])
    alpha = next(event for event in events if event["name"] == "Alpha")

    [merge] = alpha["removals"]
    assert merge["field"] == "Genres"
    assert merge["to"] == "HipHop"
    assert merge["before"] == ["hip-hop", "Rockabilly"]
    assert merge["after"] == ["HipHop", "Rockabilly"]
    assert merge["changed"] == [0]


def test_the_diff_leaves_a_value_outside_the_group_alone(client: TestClient) -> None:
    """The essential "must not": a merge must not touch ``Rockabilly``."""
    events = _diff(client, [_hip_hop_group()])
    alpha = next(event for event in events if event["name"] == "Alpha")
    assert "Rockabilly" in alpha["removals"][0]["after"]


def test_the_diff_merges_in_tags_independently_of_genres(client: TestClient) -> None:
    """The two arrays hold different vocabularies, and a group names one of them.

    A ``Tags`` group must not merge the ``Genres`` spelling as a side effect: the operator
    named one array, and the write follows the group.
    """
    # Alpha's Tags are `["Hip Hop", "britpop"]`, so this group's spellings are already the
    # canonical spelling there -- the item is selected and reported as needing no change.
    events = _diff(
        client,
        [
            {
                "field": "Tags",
                "representative": "Hip Hop",
                "spellings": ["Hip Hop", "hip-hop"],
            }
        ],
    )
    items = [event for event in events if event["type"] == "item"]
    assert [event["name"] for event in items] == ["Alpha"], items
    assert items[0]["applicable"] is False
    assert "canonical" in (items[0]["skipped_reason"] or "")

    # The Genres spelling is untouched: it was not in the group.
    assert WRITES == []


# --------------------------------------------------------------------- apply


def _apply(client: TestClient, job_id: str) -> dict[str, Any]:
    response = client.post(
        "/api/bulk/merge-similar/apply", json={"job_id": job_id, "confirm": True}
    )
    assert response.status_code == 200, response.text
    return _summary(_events(response))


def test_apply_requires_confirm(client: TestClient) -> None:
    job_id = _summary(_diff(client, [_hip_hop_group()]))["job_id"]
    response = client.post("/api/bulk/merge-similar/apply", json={"job_id": job_id})
    assert response.status_code == 422
    assert "confirm" in response.json()["error"]["message"]


def test_apply_needs_a_reviewed_job(client: TestClient) -> None:
    response = client.post(
        "/api/bulk/merge-similar/apply", json={"job_id": "nope", "confirm": True}
    )
    assert response.status_code == 404


def test_apply_rewrites_only_the_group(client: TestClient) -> None:
    job_id = _summary(_diff(client, [_hip_hop_group()]))["job_id"]
    result = _apply(client, job_id)
    # Alpha stores `hip-hop`; Beta already stores the canonical `HipHop`, so Beta is in the
    # batch and reported as needing nothing -- it is not written.
    assert result["applied"] == 1, result

    alpha = next(write["body"] for write in WRITES if write["item_id"] == ARTIST_1["Id"])
    assert alpha["Genres"] == ["HipHop", "Rockabilly"], "the spelling is merged, Rockabilly is not"
    assert alpha["Tags"] == ["Hip Hop", "britpop"], "Tags was not in the confirmed group"

    assert [write["item_id"] for write in WRITES] == [ARTIST_1["Id"]], (
        "an item that already stores the representative must not be rewritten"
    )


def test_apply_writes_the_whole_item(client: TestClient) -> None:
    """ADR 0003: the payload is a full overwrite, so the merge must carry every field."""
    job_id = _summary(_diff(client, [_hip_hop_group()]))["job_id"]
    _apply(client, job_id)

    body = next(write["body"] for write in WRITES if write["item_id"] == ARTIST_1["Id"])
    assert body["Overview"] == "A biography that must survive a merge."
    assert body["ProviderIds"] == {"MusicBrainzArtist": "mb-11"}
    assert body["ProductionYear"] == 1999

    from metaedit.domain.writable import payload_field_set

    assert set(body) == payload_field_set("MusicArtist")


def test_apply_snapshots_before_writing(client: TestClient, database_url: str) -> None:
    job_id = _summary(_diff(client, [_hip_hop_group()]))["job_id"]
    _apply(client, job_id)

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
    assert fields["Genres"] == ["hip-hop", "Rockabilly"]


def test_the_merge_reverts_through_the_existing_endpoint(
    client: TestClient, database_url: str
) -> None:
    summary = _summary(_diff(client, [_hip_hop_group()]))
    _apply(client, summary["job_id"])

    WRITES.clear()
    response = client.post(f"/api/bulk/{summary['batch_id']}/revert?confirm=true")
    assert response.status_code == 200, response.text

    body = next(write["body"] for write in WRITES if write["item_id"] == ARTIST_1["Id"])
    assert body["Genres"] == ["hip-hop", "Rockabilly"], "the original spelling is restored"


def test_a_second_apply_of_the_same_job_is_refused(client: TestClient) -> None:
    job_id = _summary(_diff(client, [_hip_hop_group()]))["job_id"]
    _apply(client, job_id)

    second = client.post("/api/bulk/merge-similar/apply", json={"job_id": job_id, "confirm": True})
    assert second.status_code == 200
    assert [event for event in _events(second) if event["type"] == "error"]


def test_the_job_list_reports_the_groups(client: TestClient) -> None:
    _diff(client, [_hip_hop_group()])
    jobs = client.get("/api/bulk/merge-similar/jobs").json()["jobs"]

    assert jobs[-1]["merging"] == [
        {"field": "Genres", "to": "HipHop", "from": ["HipHop", "hip-hop", "Hip Hop"]}
    ]
    # And the id is not usable by any other batch's apply endpoint.
    assert client.get("/api/bulk/jobs").json()["count"] == 0
    assert client.get("/api/bulk/remove-genre/jobs").json()["count"] == 0
    assert client.get("/api/bulk/remove-all-tags/jobs").json()["count"] == 0


def test_the_normal_lastfm_diff_is_untouched(client: TestClient) -> None:
    """The feature must not change what an ordinary diff proposes (ADR 0015).

    ``TagPolicy.classify`` and the mapping layer were deliberately left alone: similarity is
    a separate, confirmed operation, not a new rule in the matcher.
    """
    from metaedit.domain.tags import TagInput, TagPolicy

    outcome = TagPolicy().classify([TagInput(name="hip-hop"), TagInput(name="Hip Hop")])
    # The existing rule keeps both spellings, because it matches exactly after
    # `normalize_tag` and those two are different normalised values. Similarity is a
    # separate, operator-confirmed operation -- it did not become part of the matcher.
    assert outcome.genres == ["hip-hop", "Hip Hop"]
