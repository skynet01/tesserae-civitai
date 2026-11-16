"""Daily batches persist across renders and restarts without extra gallery calls."""

import importlib.util
import json
from datetime import datetime
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit

import pytest


def load_server():
    spec = importlib.util.spec_from_file_location(
        "daily_civitai", Path(__file__).parents[1] / "server.py"
    )
    server = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(server)
    return server


def image(ident, **changes):
    return {
        "id": ident,
        "url": f"https://image.civitai.com/x/{ident}.jpeg",
        "width": 2400,
        "height": 1600,
        "username": "artist",
        "type": "image",
        "nsfwLevel": "None",
        "browsingLevel": 1,
        "createdAt": f"2026-11-14T12:{ident % 60:02d}:00+00:00",
        "tags": [],
        **changes,
    }


@pytest.fixture
def daily(monkeypatch, tmp_path):
    server = load_server()
    clock = [datetime(2026, 11, 14, 23, 55).timestamp()]
    monkeypatch.setattr(server.time, "time", lambda: clock[0])
    calls = []
    response = [{"items": [image(i) for i in range(1, 41)]}]

    def network(url, **kwargs):
        calls.append(url)
        if isinstance(response[0], Exception):
            raise response[0]
        return response[0]

    monkeypatch.setattr(server, "fetch_json", network)
    options = {"daily_batch": True, "refresh_hours": 168}
    ctx = {
        "data_dir": str(tmp_path),
        "cell_w": 1600,
        "cell_h": 900,
        "placement_id": "art",
        "settled": True,
    }
    return server, clock, calls, response, options, ctx, network


def ids(result):
    return {entry["id"] for entry in result["images"]}


def test_daily_batch_is_newest_twenty_and_survives_restart(daily, monkeypatch):
    server, clock, calls, _, options, ctx, network = daily
    first = server.fetch(options, {}, ctx=ctx)
    assert ids(first) == set(range(21, 41))
    assert parse_qs(urlsplit(calls[0]).query)["sort"] == ["Newest"]
    # A fresh module must use the saved batch, even with a shorter refresh setting.
    restarted = load_server()
    monkeypatch.setattr(restarted, "fetch_json", network)
    clock[0] += 60
    assert ids(restarted.fetch({**options, "refresh_hours": 1}, {}, ctx=ctx)) == ids(first)
    assert len(calls) == 1


def test_next_local_day_rebuilds_and_skips_previous_ids(daily):
    server, clock, calls, response, options, ctx, _ = daily
    first = server.fetch(options, {}, ctx=ctx)
    clock[0] += 600  # Ten minutes crosses local midnight, not a 24-hour expiry.
    response[0] = {"items": [image(i) for i in range(21, 61)]}
    second = server.fetch(options, {}, ctx=ctx)
    assert ids(second) == set(range(41, 61))
    assert ids(first).isdisjoint(ids(second))
    assert len(calls) == 2
    assert ids(server.fetch(options, {}, ctx=ctx)) == ids(second)
    assert len(calls) == 2


@pytest.mark.parametrize("new_ids", [[], [51, 52]])
def test_scarce_fresh_images_use_small_batch_or_keep_previous(daily, new_ids):
    server, clock, calls, response, options, ctx, _ = daily
    first = server.fetch(options, {}, ctx=ctx)
    clock[0] += 86400
    response[0] = {"items": [image(i) for i in [*ids(first), *new_ids]]}
    result = server.fetch(options, {}, ctx=ctx)
    assert ids(result) == (set(new_ids) if new_ids else ids(first))
    assert ids(server.fetch(options, {}, ctx=ctx)) == ids(result)
    assert len(calls) == 2


@pytest.mark.parametrize("failure", [OSError("offline"), HTTPError(
    "https://civitai.com/api/v1/images", 429, "limited", {"Retry-After": "3600"}, None
)])
def test_failed_day_keeps_previous_batch_and_backs_off(daily, failure):
    server, clock, calls, response, options, ctx, _ = daily
    first = server.fetch(options, {}, ctx=ctx)
    clock[0] += 86400
    response[0] = failure
    assert ids(server.fetch(options, {}, ctx=ctx)) == ids(first)
    failed_calls = len(calls)
    clock[0] += 120
    assert ids(server.fetch(options, {}, ctx=ctx)) == ids(first)
    assert len(calls) == failed_calls
    clock[0] += 3601
    response[0] = {"items": [image(i) for i in range(41, 61)]}
    assert ids(server.fetch(options, {}, ctx=ctx)) == set(range(41, 61))


def test_disabled_option_preserves_pool_size_sort_and_hourly_expiry(daily):
    server, clock, calls, _, options, ctx, _ = daily
    options.update(daily_batch=False, sort="Most Collected")
    assert len(server.fetch(options, {}, ctx=ctx)["images"]) == 40
    assert parse_qs(urlsplit(calls[0]).query)["sort"] == ["Most Collected"]
    clock[0] += 600
    assert len(server.fetch(options, {}, ctx=ctx)["images"]) == 40
    assert len(calls) == 1


def test_daily_previews_pins_and_cache_only_listing_reuse_batch(daily):
    server, _, calls, _, options, ctx, _ = daily
    first = server.fetch(options, {}, ctx=ctx)
    history = Path(ctx["data_dir"]) / "civitai-history.json"
    original = history.read_bytes()
    assert ids(server.query_preview(options, {}, ctx=ctx)) == ids(first)
    pinned = server.fetch({**options, "pinned_image": "40"}, {}, ctx=ctx)
    assert ids(pinned) == {40}
    assert ids(server.fetch(options, {}, ctx={**ctx, "_lineup_cache_only": True})) == ids(first)
    assert history.read_bytes() == original
    assert len(calls) == 1


def test_daily_batch_filters_before_limiting_and_bounds_history(daily):
    server, clock, _, response, options, ctx, _ = daily
    options.update(resolution="native_only", min_comments=2)
    for day in range(28):
        start = day * 20 + 1
        response[0] = {"items": [
            image(9000, width=600, height=900, stats={"commentCount": 2}),
            image(9001, stats={"commentCount": 0}),
            *[image(i, stats={"commentCount": 2}) for i in range(start, start + 20)],
        ]}
        assert ids(server.fetch(options, {}, ctx=ctx)) == set(range(start, start + 20))
        clock[0] += 86400
    state = json.loads((Path(ctx["data_dir"]) / "civitai-daily.json").read_text())
    assert all(len(record["seen"]) <= 512 for record in state["pools"].values())


def test_cache_only_listing_never_builds_a_daily_batch(daily):
    server, _, calls, _, options, ctx, _ = daily
    assert server.fetch(options, {}, ctx={**ctx, "_lineup_cache_only": True})["images"] == []
    assert calls == []


def test_existing_batch_with_host_location_migrates_without_refetch(daily, monkeypatch):
    import hashlib

    server, _, calls, _, options, ctx, _ = daily
    original = server.fetch(options, {}, ctx=ctx)
    path = Path(ctx["data_dir"]) / "civitai-daily.json"
    state = json.loads(path.read_text())
    record = next(iter(state["pools"].values()))
    host_fields = {"latitude": 40.0, "longitude": -75.0, "label": "Test location"}
    legacy_key = hashlib.sha256(json.dumps(
        [host_fields, ctx["cell_w"], ctx["cell_h"], hashlib.sha256(b"").hexdigest()],
        sort_keys=True,
    ).encode()).hexdigest()
    path.write_text(json.dumps({"pools": {legacy_key: record}}))
    monkeypatch.setattr(server, "fetch_json", lambda *a, **k: pytest.fail("Refetched an existing batch"))
    assert ids(server.fetch({**options, **host_fields}, {}, ctx=ctx)) == ids(original)
    assert ids(server.fetch(options, {}, ctx={**ctx, "_lineup_cache_only": True})) == ids(original)
    assert len(calls) == 1


@pytest.mark.parametrize("mixed", [False, True])
def test_daily_collections_refresh_at_midnight_and_keep_batch_on_partial_failure(
    daily, monkeypatch, mixed
):
    server, clock, calls, _, options, ctx, _ = daily
    options.update(sources=["collections", "tags"] if mixed else ["collections"], collections=["123"])
    current = [1]
    failed = [False]

    def network(url, **kwargs):
        calls.append(url)
        if urlsplit(url).path.endswith("image.getInfinite"):
            if failed[0]:
                raise OSError("collection offline")
            payload = json.loads(parse_qs(urlsplit(url).query)["input"][0])["json"]
            assert payload["sort"] == "Newest"
            raw = image(
                current[0], nsfwLevel=1, ingestion="Scanned", blockedFor=None,
                needsReview=None, user={"username": "artist"},
            )
            return {"result": {"data": {"json": {"items": [raw]}}}}
        return {"items": [image(current[0] + 100)]}

    monkeypatch.setattr(server, "fetch_json", network)
    first = server.fetch(options, {"api_key": "test-key"}, ctx=ctx)
    assert ids(first) == ({1, 101} if mixed else {1})
    clock[0] += 600
    current[0] = 2
    assert ids(server.fetch(options, {"api_key": "test-key"}, ctx=ctx)) == ({2, 102} if mixed else {2})
    clock[0] += 86400
    current[0] = 3
    failed[0] = True
    assert ids(server.fetch(options, {"api_key": "test-key"}, ctx=ctx)) == ({2, 102} if mixed else {2})
    attempted = len(calls)
    clock[0] += 120
    server.fetch(options, {"api_key": "test-key"}, ctx=ctx)
    assert len(calls) == attempted


def test_failure_of_an_empty_collection_does_not_finalize_a_partial_day(daily, monkeypatch):
    server, clock, _, _, options, ctx, _ = daily
    options.update(sources=["collections"], collections=["123", "456"])
    current = [1]

    def network(url, **kwargs):
        payload = json.loads(parse_qs(urlsplit(url).query)["input"][0])["json"]
        if payload["collectionId"] == 123:
            if current[0] == 2:
                raise OSError("empty collection offline")
            items = []
        else:
            items = [image(current[0], nsfwLevel=1, ingestion="Scanned")]
        return {"result": {"data": {"json": {"items": items}}}}

    monkeypatch.setattr(server, "fetch_json", network)
    assert ids(server.fetch(options, {"api_key": "test-key"}, ctx=ctx)) == {1}
    clock[0] += 86400
    current[0] = 2
    assert ids(server.fetch(options, {"api_key": "test-key"}, ctx=ctx)) == {1}
