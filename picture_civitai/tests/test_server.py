"""Fake-network tests for Civitai selection, cache isolation and request bounds."""

import importlib.util
import json
import threading
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit

import pytest


@pytest.fixture
def server():
    path = Path(__file__).parents[1] / "server.py"
    assert path.exists(), "Civitai backend is not implemented"
    spec = importlib.util.spec_from_file_location("civitai_server", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def image(ident=1, *, tags=None, **changes):
    entry = {
        "id": ident,
        "url": f"https://image.civitai.com/x/image-{ident}.jpeg",
        "width": 2048,
        "height": 1536,
        "username": "alice",
        "nsfwLevel": "None",
        "browsingLevel": 1,
        "type": "image",
        "tags": [{"id": 10, "name": "landscape"}] if tags is None else tags,
    }
    entry.update(changes)
    return entry


def test_drawing_style_uses_verified_image_tag_without_discovery(
    server, monkeypatch, tmp_path
):
    def response(url, query):
        if query.get("tags") == ["81"]:
            return {"items": [image(tags=[{"id": 81, "name": "drawing"}])]}
        return {"items": []}

    calls = fake_network(monkeypatch, server, response)
    result = server.fetch(
        {"positive_tags": "drawing"}, {}, ctx={"data_dir": str(tmp_path)}
    )
    assert len(result["images"]) == 1
    assert len(calls) == 1
    assert parse_qs(urlsplit(calls[0]).query)["tags"] == ["81"]


def fake_network(monkeypatch, server, callback):
    calls = []

    def fetch(url, **kwargs):
        assert urlsplit(url).scheme == "https"
        assert urlsplit(url).hostname == "civitai.com"
        assert 0 < kwargs["timeout"] <= 2
        assert kwargs["max_bytes"] <= 2 * 1024 * 1024
        calls.append(url)
        return callback(url, parse_qs(urlsplit(url).query))

    monkeypatch.setattr(server, "fetch_json", fetch)
    return calls


def test_public_feed_has_native_dimensions_and_fixed_safe_query(
    server, monkeypatch, tmp_path
):
    calls = fake_network(monkeypatch, server, lambda *_: {"items": [image()]})
    result = server.fetch({}, {}, ctx={"data_dir": str(tmp_path)})
    assert result["images"] == [
        {
            "id": 1,
            "url": "https://image.civitai.com/x/image-1.jpeg",
            "width": 2048,
            "height": 1536,
            "username": "alice",
            "permalink": "https://civitai.com/images/1",
            "tags": [{"id": 10, "name": "landscape"}],
        }
    ]
    query = parse_qs(urlsplit(calls[0]).query)
    assert query["browsingLevel"] == ["1"]
    assert query["withTags"] == ["true"]
    assert query["type"] == ["image"]
    assert query["sort"] == ["Most Reactions"]
    assert query["period"] == ["Week"]


def test_creator_restriction_and_any_positive_tags_are_locally_verified(
    server, monkeypatch, tmp_path
):
    entries = [
        image(),
        image(2, tags=[{"id": 20, "name": "forest"}]),
        image(3, username="mallory"),
        image(4, tags=[]),
    ]
    calls = fake_network(monkeypatch, server, lambda *_: {"items": entries})
    result = server.fetch(
        {"positive_tags": "10,20", "users": "alice"},
        {},
        ctx={"data_dir": str(tmp_path)},
    )
    assert [entry["id"] for entry in result["images"]] == [1, 2]
    assert {parse_qs(urlsplit(url).query)["tags"][0] for url in calls} == {"10", "20"}
    assert all(parse_qs(urlsplit(url).query)["username"] == ["alice"] for url in calls)


def test_all_tags_and_exclusions_fail_closed_for_missing_tags(
    server, monkeypatch, tmp_path
):
    entries = [
        image(),
        image(2, tags=[{"id": 10, "name": "landscape"}, {"id": 20, "name": "forest"}]),
        image(
            3,
            tags=[
                {"id": 10, "name": "landscape"},
                {"id": 20, "name": "forest"},
                {"id": 30, "name": "person"},
            ],
        ),
        image(4, tags=None),
    ]
    entries[-1].pop("tags")
    calls = fake_network(monkeypatch, server, lambda *_: {"items": entries})
    result = server.fetch(
        {"positive_tags": "10,20", "negative_tags": "30", "tag_match": "all"},
        {},
        ctx={"data_dir": str(tmp_path)},
    )
    assert [entry["id"] for entry in result["images"]] == [2]
    assert parse_qs(urlsplit(calls[0]).query)["tags"] == ["10,20"]


def test_tag_names_resolve_exactly_from_image_tags_and_cache(
    server, monkeypatch, tmp_path
):
    def response(url, query):
        if "/trpc/" in url:
            request = json.loads(query["input"][0])["json"]
            assert request["entityType"] == ["Image"]
            assert request["query"] == "landscape"
            return {
                "result": {
                    "data": {
                        "json": {
                            "items": [
                                {"id": 99, "name": "landscape painting"},
                                {"id": 10, "name": "Landscape"},
                            ]
                        }
                    }
                }
            }
        return {"items": [image()]}

    calls = fake_network(monkeypatch, server, response)
    options = {"positive_tags": " LANDSCAPE "}
    assert server.fetch(
        options, {"api_key": "private-key"}, ctx={"data_dir": str(tmp_path)}
    )["images"]
    assert server.fetch(
        {**options, "period": "Day"},
        {"api_key": "private-key"},
        ctx={"data_dir": str(tmp_path)},
    )["images"]
    assert len([url for url in calls if "/trpc/" in url]) == 1


def test_unknown_positive_tag_name_is_an_error_without_unfiltered_feed(
    server, monkeypatch, tmp_path
):
    calls = fake_network(monkeypatch, server, lambda *_: {"items": [image()]})
    result = server.fetch(
        {"positive_tags": "unknown tag"}, {}, ctx={"data_dir": str(tmp_path)}
    )
    assert result["images"] == []
    assert "unknown tag" in result["error"].lower()
    assert "numeric" in result["error"].lower()
    assert len(calls) == 1
    assert "/trpc/" not in calls[0]


def test_public_sample_discovers_positive_names_without_authentication(
    server, monkeypatch, tmp_path
):
    calls = fake_network(monkeypatch, server, lambda *_: {"items": [image()]})
    result = server.fetch(
        {"positive_tags": "landscape"}, {}, ctx={"data_dir": str(tmp_path)}
    )
    assert [item["id"] for item in result["images"]] == [1]
    assert len(calls) == 2
    assert parse_qs(urlsplit(calls[-1]).query)["tags"] == ["10"]
    assert "/trpc/" not in calls[0]


def test_negative_names_are_applied_locally_without_lookup(
    server, monkeypatch, tmp_path
):
    calls = fake_network(
        monkeypatch,
        server,
        lambda *_: {
            "items": [
                image(),
                image(2, tags=[]),
                image(3, tags=[{"id": 20, "name": "forest"}]),
            ]
        },
    )
    result = server.fetch(
        {"negative_tags": "LANDSCAPE,unknown exclusion"},
        {},
        ctx={"data_dir": str(tmp_path)},
    )
    assert [item["id"] for item in result["images"]] == [3]
    assert len(calls) == 1


@pytest.mark.parametrize(
    "changes",
    [
        {"url": "http://image.civitai.com/a.jpg"},
        {"url": "https://image.civitai.com.evil.test/a.jpg"},
        {"url": "https://user@image.civitai.com/a.jpg"},
        {"url": "https://image.civitai.com:444/a.jpg"},
        {"url": "https://image.civitai.com/a.svg"},
        {"url": "https://image.civitai.com/a.jpg\n"},
        {"width": 0},
        {"height": "1536"},
        {"id": "../../admin"},
        {"nsfwLevel": 2},
        {"type": "video"},
    ],
)
def test_invalid_or_unsafe_image_is_discarded(server, monkeypatch, tmp_path, changes):
    fake_network(monkeypatch, server, lambda *_: {"items": [image(**changes)]})
    assert server.fetch({}, {}, ctx={"data_dir": str(tmp_path)})["images"] == []


def test_default_refresh_stops_after_one_page_even_with_cursor(
    server, monkeypatch, tmp_path
):
    def response(url, query):
        ident = 2 if "cursor" in query else 1
        return {
            "items": [image(ident)],
            "metadata": {"nextCursor": "abc", "nextPage": "http://127.0.0.1/admin"},
        }

    calls = fake_network(monkeypatch, server, response)
    result = server.fetch({}, {}, ctx={"data_dir": str(tmp_path)})
    assert [entry["id"] for entry in result["images"]] == [1]
    assert len(calls) == 1
    assert all("page" not in parse_qs(urlsplit(url).query) for url in calls)


def test_cache_reused_for_layout_changes_but_not_filter_changes(
    server, monkeypatch, tmp_path
):
    clock = [100000.0]
    monkeypatch.setattr(server.time, "time", lambda: clock[0])
    calls = fake_network(monkeypatch, server, lambda *_: {"items": [image()]})
    ctx = {"data_dir": str(tmp_path)}
    assert server.fetch({"positive_tags": "10"}, {}, ctx=ctx)["images"]
    assert server.fetch(
        {"positive_tags": "10", "orientation": "portrait", "rotation_minutes": 10},
        {},
        ctx=ctx,
    )["images"]
    assert len(calls) == 1
    clock[0] += 90000
    monkeypatch.setattr(
        server, "fetch_json", lambda *a, **k: (_ for _ in ()).throw(TimeoutError())
    )
    stale = server.fetch({"positive_tags": "10"}, {}, ctx=ctx)
    assert stale["stale"] is True
    assert stale["images"]
    changed = server.fetch({"positive_tags": "10", "negative_tags": "20"}, {}, ctx=ctx)
    assert changed["images"] == []
    assert changed.get("stale") is not True


def test_rate_limit_has_cooldown_without_retries_or_repeated_requests(
    server, monkeypatch, tmp_path
):
    def limited(*_):
        raise HTTPError(
            "https://civitai.com/api/v1/images",
            429,
            "Too Many Requests",
            {"Retry-After": "120"},
            None,
        )

    calls = fake_network(monkeypatch, server, limited)
    ctx = {"data_dir": str(tmp_path)}
    result = server.fetch({}, {}, ctx=ctx)
    assert result["images"] == []
    assert "rate" in result["error"].lower()
    assert server.fetch({}, {}, ctx=ctx)["images"] == []
    assert len(calls) == 1


def test_cursor_and_candidate_pool_are_bounded(server, monkeypatch, tmp_path):
    def response(url, query):
        number = int(query.get("cursor", ["0"])[0])
        return {
            "items": [image(number + 1, tags=[])],
            "metadata": {"nextCursor": str(number + 1)},
        }

    calls = fake_network(monkeypatch, server, response)
    assert len(server.fetch({}, {}, ctx={"data_dir": str(tmp_path)})["images"]) <= 100
    assert len(calls) <= 8


def test_disk_cache_is_bounded_and_corruption_is_ignored(server, monkeypatch, tmp_path):
    (tmp_path / "civitai-cache.json").write_text("invalid")
    fake_network(monkeypatch, server, lambda *_: {"items": [image()]})
    for period in ["Day", "Week", "Month", "AllTime"]:
        for sort in ["Newest", "Most Reactions"]:
            server.fetch(
                {"period": period, "sort": sort}, {}, ctx={"data_dir": str(tmp_path)}
            )
    server.fetch({"users": "alice"}, {}, ctx={"data_dir": str(tmp_path)})
    assert (
        sum(path.stat().st_size for path in tmp_path.iterdir() if path.is_file())
        < 2 * 1024 * 1024
    )
    assert len(list(tmp_path.iterdir())) <= 2


def test_all_tag_sources_contribute_even_when_first_fills_pool(
    server, monkeypatch, tmp_path
):
    def response(url, query):
        tag = int(query["tags"][0])
        return {
            "items": [
                image(
                    tag * 1000 + number,
                    tags=[{"id": tag, "name": str(tag)}],
                    width=2000 if tag == 20 else 1000,
                    height=1000 if tag == 20 else 2000,
                )
                for number in range(100)
            ]
        }

    calls = fake_network(monkeypatch, server, response)
    result = server.fetch(
        {"positive_tags": "10,20"}, {}, ctx={"data_dir": str(tmp_path)}
    )
    assert len(result["images"]) == 100
    assert {entry["width"] for entry in result["images"]} == {1000, 2000}
    assert len(calls) == 2


def test_secret_setting_is_used_only_in_authorization_and_never_saved(
    server, monkeypatch, tmp_path
):
    def fetch(url, **kwargs):
        assert kwargs["headers"]["Authorization"] == "Bearer private-secret"
        assert "private-secret" not in url
        return {"items": [image()]}

    monkeypatch.setattr(server, "fetch_json", fetch)
    result = server.fetch(
        {},
        {"api_key_secret": "private-secret"},
        ctx={"data_dir": str(tmp_path), "placement_id": "page/cell@frame"},
    )
    assert result["placement_id"] == "page/cell@frame"
    assert result["images"]
    assert "private-secret" not in json.dumps(result)
    assert "private-secret" not in (tmp_path / "civitai-cache.json").read_text()


def test_incomplete_tag_metadata_cannot_bypass_exclusions(
    server, monkeypatch, tmp_path
):
    entries = [image(tags=[{"id": 10, "name": "landscape"}, {"name": "person"}])]
    fake_network(monkeypatch, server, lambda *_: {"items": entries})
    assert (
        server.fetch({"negative_tags": "person"}, {}, ctx={"data_dir": str(tmp_path)})[
            "images"
        ]
        == []
    )


@pytest.mark.parametrize(
    "contents",
    [
        {"pools": {}, "tags": {}, "retry_at": "corrupt"},
        {
            "pools": {"unrelated": {"fetched": []}},
            "tags": {"public:landscape": {"id": 10, "at": "corrupt"}},
        },
    ],
)
def test_structurally_corrupt_cache_does_not_break_render(
    server, monkeypatch, tmp_path, contents
):
    (tmp_path / "civitai-cache.json").write_text(json.dumps(contents))
    fake_network(monkeypatch, server, lambda *_: {"items": [image()]})
    assert server.fetch(
        {"positive_tags": "landscape"}, {}, ctx={"data_dir": str(tmp_path)}
    )["images"]


def test_api_key_change_does_not_reuse_tag_lookup_or_pool(
    server, monkeypatch, tmp_path
):
    keys = []

    def fetch(url, **kwargs):
        keys.append(kwargs["headers"]["Authorization"])
        if "/trpc/" in url:
            return {
                "result": {
                    "data": {"json": {"items": [{"id": 10, "name": "landscape"}]}}
                }
            }
        return {"items": [image()]}

    monkeypatch.setattr(server, "fetch_json", fetch)
    ctx = {"data_dir": str(tmp_path)}
    server.fetch({"positive_tags": "landscape"}, {"api_key_secret": "first"}, ctx=ctx)
    server.fetch({"positive_tags": "landscape"}, {"api_key_secret": "second"}, ctx=ctx)
    assert keys == ["Bearer first", "Bearer first", "Bearer second", "Bearer second"]


def test_empty_filtered_page_uses_only_one_bounded_cursor_retry(
    server, monkeypatch, tmp_path
):
    def response(url, query):
        return {
            "items": [image(tags=[])],
            "metadata": {"nextCursor": "abc", "nextPage": "http://127.0.0.1/admin"},
        }

    calls = fake_network(monkeypatch, server, response)
    result = server.fetch({"positive_tags": "10"}, {}, ctx={"data_dir": str(tmp_path)})
    assert result["images"] == []
    assert len(calls) == 2
    assert parse_qs(urlsplit(calls[1]).query)["cursor"] == ["abc"]


def test_late_landscapes_survive_candidate_pool_truncation(
    server, monkeypatch, tmp_path
):
    entries = [image(number + 1, width=1000, height=2000) for number in range(194)]
    entries.extend(image(number + 1000, width=2000, height=1000) for number in range(6))
    fake_network(monkeypatch, server, lambda *_: {"items": entries})
    result = server.fetch({}, {}, ctx={"data_dir": str(tmp_path)})
    assert len(result["images"]) == 100
    assert sum(item["width"] > item["height"] for item in result["images"]) == 6


def test_refresh_budget_reduces_each_request_timeout_and_stops(
    server, monkeypatch, tmp_path
):
    clock = [0.0]
    monkeypatch.setattr(server.time, "monotonic", lambda: clock[0])
    observed = []

    def fetch(url, **kwargs):
        observed.append(kwargs["timeout"])
        clock[0] += 1.8
        return {"items": [image()]}

    monkeypatch.setattr(server, "fetch_json", fetch)
    result = server.fetch(
        {"positive_tags": "10,20,30,40"}, {}, ctx={"data_dir": str(tmp_path)}
    )
    assert len(observed) <= 3
    assert all(value <= 2 for value in observed)
    assert observed[-1] <= 0.5
    assert result.get("error")


def test_concurrent_placement_returns_same_filter_stale_without_waiting(
    server, monkeypatch, tmp_path
):
    now = [100000.0]
    monkeypatch.setattr(server.time, "time", lambda: now[0])
    fake_network(monkeypatch, server, lambda *_: {"items": [image()]})
    ctx = {"data_dir": str(tmp_path)}
    server.fetch({"positive_tags": "10"}, {}, ctx=ctx)
    now[0] += 90000
    started, release = threading.Event(), threading.Event()

    def slow(*args, **kwargs):
        started.set()
        assert release.wait(2)
        return {"items": [image(2)]}

    monkeypatch.setattr(server, "fetch_json", slow)
    result = []
    first = threading.Thread(
        target=lambda: result.append(server.fetch({"positive_tags": "10"}, {}, ctx=ctx))
    )
    first.start()
    assert started.wait(1)
    try:
        begin = time.perf_counter()
        busy = server.fetch({"positive_tags": "10"}, {}, ctx=ctx)
        changed = server.fetch({"positive_tags": "20"}, {}, ctx=ctx)
        assert time.perf_counter() - begin < 0.2
        assert [entry["id"] for entry in busy["images"]] == [1]
        assert busy["stale"] is True
        assert changed["images"] == []
        assert "updat" in changed["error"].lower()
    finally:
        release.set()
        first.join(2)
    assert result and result[0]["images"][0]["id"] == 2


def test_slow_transport_returns_updating_then_reuses_completed_refresh(
    server, monkeypatch, tmp_path
):
    monkeypatch.setattr(server, "MAX_RENDER_WAIT_S", 0.05, raising=False)
    started, release = threading.Event(), threading.Event()
    calls = []

    def slow(*args, **kwargs):
        calls.append(1)
        started.set()
        assert release.wait(2)
        return {"items": [image()]}

    monkeypatch.setattr(server, "fetch_json", slow)
    ctx = {"data_dir": str(tmp_path)}
    begin = time.perf_counter()
    try:
        result = server.fetch({}, {}, ctx=ctx)
        assert time.perf_counter() - begin < 0.3
        assert started.is_set()
        assert result["images"] == []
        assert "updat" in result["error"].lower()
    finally:
        release.set()
    for _ in range(100):
        if (tmp_path / "civitai-cache.json").exists():
            break
        time.sleep(0.005)
    assert server.fetch({}, {}, ctx=ctx)["images"]
    assert len(calls) == 1


def test_verified_style_tags_need_only_one_page_each_daily(
    server, monkeypatch, tmp_path
):
    now = [100000.0]
    monkeypatch.setattr(server.time, "time", lambda: now[0])

    def response(url, query):
        assert "/trpc/" not in url
        tag = int(query["tags"][0])
        assert tag in (5145, 7660)
        return {
            "items": [
                image(
                    tag,
                    tags=[
                        {
                            "id": tag,
                            "name": "illustration" if tag == 5145 else "painting",
                        }
                    ],
                )
            ],
            "metadata": {"nextCursor": "unused"},
        }

    calls = fake_network(monkeypatch, server, response)
    options = {"positive_tags": "illustration, painting"}
    ctx = {"data_dir": str(tmp_path)}
    assert len(server.fetch(options, {}, ctx=ctx)["images"]) == 2
    assert len(calls) == 2
    assert (
        len(server.fetch({**options, "orientation": "portrait"}, {}, ctx=ctx)["images"])
        == 2
    )
    assert len(calls) == 2
    now[0] += 86401
    assert len(server.fetch(options, {}, ctx=ctx)["images"]) == 2
    assert len(calls) == 4


def test_refresh_thread_preserves_host_network_context(server, monkeypatch, tmp_path):
    import contextvars

    capability = contextvars.ContextVar("network_capability", default="denied")
    token = capability.set("allowed")

    def fetch(*args, **kwargs):
        assert capability.get() == "allowed"
        return {"items": [image()]}

    monkeypatch.setattr(server, "fetch_json", fetch)
    try:
        assert server.fetch({}, {}, ctx={"data_dir": str(tmp_path)})["images"]
    finally:
        capability.reset(token)


def test_pending_refresh_requests_preview_polling_then_stops(
    server, monkeypatch, tmp_path
):
    monkeypatch.setattr(server, "MAX_RENDER_WAIT_S", 0.02)
    started, release, finished = threading.Event(), threading.Event(), threading.Event()

    def slow(*args, **kwargs):
        started.set()
        assert release.wait(2)
        return {"items": [image()]}

    monkeypatch.setattr(server, "fetch_json", slow)
    ctx = {"data_dir": str(tmp_path)}
    try:
        assert server.fetch({}, {}, ctx=ctx)["live_refresh_s"] == 2
        assert started.is_set()
        assert server.fetch({}, {}, ctx=ctx)["live_refresh_s"] == 2
    finally:
        release.set()
    for _ in range(100):
        if not server._LOCK.locked():
            finished.set()
            break
        time.sleep(0.005)
    assert finished.is_set()
    assert "live_refresh_s" not in server.fetch({}, {}, ctx=ctx)


def test_physical_target_dimensions_are_per_render_not_cache_identity(
    server, monkeypatch, tmp_path
):
    calls = fake_network(monkeypatch, server, lambda *_: {"items": [image()]})
    base = {"data_dir": str(tmp_path)}
    first = server.fetch({}, {}, ctx={**base, "cell_w": 2560, "cell_h": 1440})
    second = server.fetch({}, {}, ctx={**base, "cell_w": 1440, "cell_h": 2560})
    assert (first["target_w"], first["target_h"]) == (2560, 1440)
    assert (second["target_w"], second["target_h"]) == (1440, 2560)
    assert len(calls) == 1
    missing = server.fetch({}, {}, ctx={**base, "cell_w": "2560", "cell_h": 0})
    assert "target_w" not in missing and "target_h" not in missing
    stored = (tmp_path / "civitai-cache.json").read_text()
    assert "target_w" not in stored and "target_h" not in stored


def test_guarded_stream_read_deadline_releases_lock_for_another_collection(
    server, monkeypatch, tmp_path
):
    from types import SimpleNamespace

    clock = [0.0]
    monkeypatch.setattr(server.time, "monotonic", lambda: clock[0])
    opened, timeouts, closed = [], [], []

    class Response:
        fp = SimpleNamespace(
            raw=SimpleNamespace(
                _sock=SimpleNamespace(settimeout=lambda value: timeouts.append(value))
            )
        )

        def __enter__(self):
            return self

        def __exit__(self, *args):
            closed.append(True)

        def read1(self, size):
            assert 0 < size <= 65536
            clock[0] += 0.7
            return b" "

    class Opener:
        def open(self, request, timeout):
            opened.append((request, timeout))
            return Response()

    monkeypatch.setattr(
        server, "guarded_opener", lambda **kwargs: Opener(), raising=False
    )
    ctx = {"data_dir": str(tmp_path)}
    failed = server.fetch({}, {"api_key_secret": "private-key"}, ctx=ctx)
    assert failed["images"] == []
    assert "error" in failed
    assert "live_refresh_s" not in failed
    assert not server._LOCK.locked()
    assert clock[0] <= 2.8
    assert closed == [True]
    assert timeouts and all(value <= 2 for value in timeouts)
    request, timeout = opened[0]
    assert request.get_header("Authorization") == "Bearer private-key"
    assert request.full_url.startswith("https://civitai.com/api/v1/images?")
    fake_network(monkeypatch, server, lambda *_: {"items": [image()]})
    assert server.fetch({"period": "Day"}, {}, ctx=ctx)["images"]


def test_json_transport_refuses_other_hosts_before_opening(server, monkeypatch):
    def unexpected(**kwargs):
        pytest.fail("An arbitrary URL reached the network opener")

    monkeypatch.setattr(server, "guarded_opener", unexpected, raising=False)
    with pytest.raises(ValueError):
        server.fetch_json(
            "https://example.com/api/v1/images",
            headers={"Authorization": "Bearer private-key"},
        )


@pytest.mark.parametrize("oversized", [False, True])
def test_guarded_json_transport_handles_eof_and_enforces_two_mib_cap(
    server, monkeypatch, oversized
):
    from types import SimpleNamespace

    payload = b" " * (2 * 1024 * 1024 + 1) if oversized else b'{"items":[]}'
    closed, positions = [], [0]
    socket = SimpleNamespace(settimeout=lambda value: None)

    class Response:
        fp = SimpleNamespace(raw=SimpleNamespace(_sock=socket))

        def __enter__(self):
            return self

        def __exit__(self, *args):
            closed.append(True)

        def isclosed(self):
            return self.fp is None

        def read1(self, size):
            assert size <= 65536
            chunk = payload[positions[0] : positions[0] + size]
            positions[0] += len(chunk)
            if positions[0] == len(payload):
                self.fp = None  # HTTPResponse closes its buffer at Content-Length EOF.
            return chunk

    response = Response()
    opener = SimpleNamespace(open=lambda *args, **kwargs: response)
    monkeypatch.setattr(server, "guarded_opener", lambda **kwargs: opener)
    if oversized:
        with pytest.raises(ValueError, match="byte cap"):
            server.fetch_json("https://civitai.com/api/v1/images")
    else:
        assert server.fetch_json("https://civitai.com/api/v1/images") == {"items": []}
    assert closed == [True]


def test_api_redirect_never_sends_key_to_another_host(server):
    from urllib.request import Request

    request = Request(
        "https://civitai.com/api/v1/images",
        headers={"Authorization": "Bearer private-key"},
    )
    with pytest.raises(ValueError):
        server._ApiRedirect().redirect_request(
            request, None, 302, "Found", {}, "https://example.com/api/v1/images"
        )
