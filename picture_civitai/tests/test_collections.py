"""Own and followed collections join the cached, frame-eligible artwork pool."""

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from flask import Flask


def collection_image(ident=101, **changes):
    return {
        "id": ident,
        "url": "3aceec32-6933-46aa-9dca-7e96793035d2",
        "width": 4000,
        "height": 2500,
        "type": "image",
        "nsfwLevel": 1,
        "ingestion": "Scanned",
        "blockedFor": None,
        "needsReview": None,
        "user": {"username": "curated_artist"},
        "tags": [{"id": 7660, "name": "painting"}],
        "stats": {
            "likeCountAllTime": 10,
            "heartCountAllTime": 2,
            "cryCountAllTime": 0,
            "laughCountAllTime": 0,
            "commentCountAllTime": 3,
        },
        **changes,
    }


@pytest.fixture
def gallery(monkeypatch, tmp_path):
    folder = Path(__file__).parents[1]
    spec = importlib.util.spec_from_file_location(
        "collection_server", folder / "server.py"
    )
    server = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(server)
    manifest = json.loads((folder / "plugin.json").read_text())
    settings = {"api_key": "private-test-key"}
    options = {
        field["name"]: field.get("default") for field in manifest["cell_options"]
    }
    options.update(
        sources=["collections"],
        collections=["18707243"],
        min_reactions=5,
        resolution="native_only",
        positive_tags="unmatched tag",
        users="ignored",
    )
    ctx = {
        "data_dir": str(tmp_path),
        "cell_w": 3840,
        "cell_h": 2160,
        "placement_id": "art",
        "preview": True,
    }
    app = Flask(__name__)
    app.config["PLUGIN_REGISTRY"] = {
        "picture_civitai": SimpleNamespace(data_dir=tmp_path, manifest=manifest)
    }
    app.config["SETTINGS_STORE"] = SimpleNamespace(
        get_for_runtime=lambda *a: dict(settings)
    )
    calls = []

    def network(url, **kwargs):
        parsed = urlsplit(url)
        params = parse_qs(parsed.query)
        payload = (
            json.loads(params["input"][0])["json"] if "input" in params else params
        )
        calls.append((parsed.path, payload, kwargs))
        if parsed.path.endswith("collection.getAllUser"):
            data = [
                {"id": 18707243, "name": "Cool art", "type": "Image", "isOwner": True},
                {
                    "id": 55,
                    "name": "Landscapes",
                    "type": "Image",
                    "isOwner": False,
                    "ownerUsername": "alice",
                },
                {"id": 99, "name": "Models", "type": "Model", "isOwner": True},
            ]
        elif parsed.path.endswith("image.getInfinite"):
            data = {
                "items": [
                    collection_image(),
                    collection_image(102, width=2500, height=4000),
                    collection_image(103, nsfwLevel=8),
                    collection_image(104, tags=[{"id": 9, "name": "photography"}]),
                ],
                "nextCursor": None,
            }
        elif parsed.path == "/api/v1/images":
            return {
                "items": [
                    {
                        "id": 1,
                        "url": "https://image.civitai.com/x/1/width=4000/art.jpg",
                        "width": 4000,
                        "height": 2500,
                        "type": "image",
                        "browsingLevel": 1,
                        "nsfwLevel": "None",
                        "username": "alice",
                        "tags": [{"id": 7660, "name": "painting"}],
                        "stats": {
                            "likeCount": 10,
                            "heartCount": 0,
                            "cryCount": 0,
                            "laughCount": 0,
                            "commentCount": 3,
                        },
                    }
                ]
            }
        else:
            pytest.fail(f"Unexpected request path {parsed.path}")
        return {"result": {"data": {"json": data}}}

    monkeypatch.setattr(server, "fetch_json", network)
    with app.app_context():
        yield server, options, settings, ctx, calls, network


def test_choices_include_own_and_followed_image_collections_and_cache(gallery):
    server, _, settings, _, calls, _ = gallery
    rows = server.choices("collections")
    assert rows == [
        {"value": "18707243", "label": "Cool art (yours)"},
        {"value": "55", "label": "Landscapes (followed, by alice)"},
    ]
    assert calls[0][1] == {
        "type": "Image",
        "permission": "VIEW",
        "contributingOnly": True,
    }
    assert calls[0][2]["headers"]["Authorization"] == "Bearer private-test-key"
    assert server.choices("collections") == rows and len(calls) == 1
    settings["api_key"] = "different-identity"
    server.choices("collections")
    assert len(calls) == 2


def test_collection_only_uses_private_read_endpoint_and_shared_filters(gallery):
    server, options, settings, ctx, calls, _ = gallery
    options = {**options, "positive_tags": "a,b,c,d,e,f,g", "users": "a,b,c,d,e"}
    result = server.query_preview(options, settings, ctx=ctx)
    assert [image["id"] for image in result["images"]] == [101]
    assert result["images"][0]["matches"] == ["Collection: 18707243"]
    assert result["images"][0]["url"].startswith("https://image.civitai.com/")
    assert calls[0][1]["collectionId"] == 18707243
    assert calls[0][1]["include"] == ["tags"]
    assert len(calls) == 1
    assert server.query_preview(options, settings, ctx=ctx)["total"] == 1
    assert len(calls) == 1
    assert (
        server.query_preview({**options, "min_reactions": 1000}, settings, ctx=ctx)[
            "total"
        ]
        == 0
    )
    assert len(calls) == 1
    assert not (Path(ctx["data_dir"]) / "civitai-history.json").exists()


def test_mixed_sources_merge_and_disabled_sources_make_no_requests(gallery):
    server, options, settings, ctx, calls, _ = gallery
    options = {**options, "positive_tags": "painting", "users": "alice"}
    result = server.query_preview(
        {**options, "sources": ["tags", "creators", "collections"]}, settings, ctx=ctx
    )
    assert {image["id"] for image in result["images"]} == {1, 101}
    assert any(path == "/api/v1/images" for path, _, _ in calls)
    calls.clear()
    result = server.fetch({**options, "sources": []}, settings, ctx=ctx)
    assert result["images"] == [] and "source" in result["error"].lower()
    assert calls == []


def test_collection_lineups_and_pins_are_cache_only_and_preserve_history(
    gallery, monkeypatch
):
    server, options, settings, ctx, calls, _ = gallery
    server.fetch(options, settings, ctx=ctx)
    count = len(calls)
    monkeypatch.setattr(
        server, "fetch_json", lambda *a, **k: pytest.fail("Network from cached listing")
    )
    assert [row["key"] for row in server.lineup_items(options, ctx=ctx)] == ["101"]
    pin = server.fetch(
        {**options, "pinned_image": "101"}, settings, ctx={**ctx, "preview": False}
    )
    assert pin["selected_id"] == 101
    assert len(calls) == count
    assert not (Path(ctx["data_dir"]) / "civitai-history.json").exists()


def test_collection_failure_does_not_hide_working_creator_pool(gallery, monkeypatch):
    server, options, settings, ctx, calls, network = gallery

    def fail_collection(url, **kwargs):
        if "image.getInfinite" in url:
            raise TimeoutError("unavailable")
        return network(url, **kwargs)

    monkeypatch.setattr(server, "fetch_json", fail_collection)
    result = server.query_preview(
        {**options, "sources": ["creators", "collections"], "users": "alice"},
        settings,
        ctx=ctx,
    )
    assert [image["id"] for image in result["images"]] == [1]
    assert "collection" in result["warning"].lower()


@pytest.mark.parametrize(
    "changes",
    [
        {"nsfwLevel": 2},
        {"type": "video"},
        {"ingestion": "Pending"},
        {"blockedFor": "reason"},
        {"needsReview": "pending"},
        {"url": "https://evil.test/a"},
    ],
)
def test_collection_images_reject_ineligible_media(gallery, monkeypatch, changes):
    server, options, settings, ctx, _, _ = gallery
    monkeypatch.setattr(
        server,
        "fetch_json",
        lambda *a, **k: {
            "result": {
                "data": {
                    "json": {"items": [collection_image(**changes)], "nextCursor": None}
                }
            }
        },
    )
    assert server.fetch(options, settings, ctx=ctx)["images"] == []


def test_collection_cache_is_isolated_by_key_and_reuses_raw_pool_for_filter_changes(
    gallery, monkeypatch
):
    server, options, settings, ctx, calls, _ = gallery
    assert server.query_preview(options, settings, ctx=ctx)["total"] == 1
    assert (
        server.query_preview(
            {**options, "negative_tags": "painting, photography"}, settings, ctx=ctx
        )["total"]
        == 0
    )
    assert len(calls) == 1
    monkeypatch.setattr(
        server, "fetch_json", lambda *a, **k: pytest.fail("Cache-only lookup fetched")
    )
    changed = server.fetch(
        options, {"api_key": "different-key"}, ctx={**ctx, "_lineup_cache_only": True}
    )
    assert changed["images"] == []


@pytest.mark.parametrize("sources", [["creators"], ["tags"]])
def test_disabled_tag_or_creator_sources_do_not_issue_searches(gallery, sources):
    server, options, settings, ctx, calls, _ = gallery
    result = server.fetch(
        {
            **options,
            "sources": sources,
            "users": "alice",
            "positive_tags": "painting" if sources == ["tags"] else "unknown tag",
        },
        settings,
        ctx=ctx,
    )
    assert [image["id"] for image in result["images"]] == [1]
    assert len(calls) == 1
    params = calls[0][1]
    assert ("username" in params) == (sources == ["creators"])
    assert ("tags" in params) == (sources == ["tags"])


def test_own_and_followed_collection_overlap_is_deduplicated(gallery):
    server, options, settings, ctx, calls, _ = gallery
    result = server.query_preview(
        {**options, "collections": ["18707243", "55"]}, settings, ctx=ctx
    )
    assert len(result["images"]) == 1
    assert set(result["images"][0]["collection_ids"]) == {"18707243", "55"}
    assert len(calls) == 2


def test_collection_picker_serves_stale_choices_and_backs_off(gallery, monkeypatch):
    server, _, settings, _, calls, _ = gallery
    rows = server.choices("collections")
    now = server.time.time()
    monkeypatch.setattr(server.time, "time", lambda: now + 901)
    attempts = []

    def offline(*a, **k):
        attempts.append(True)
        raise TimeoutError("unavailable")

    monkeypatch.setattr(server, "fetch_json", offline)
    assert all(row in server.choices("collections") for row in rows)
    assert all(row in server.choices("collections") for row in rows)
    assert len(attempts) == 1
    settings["api_key"] = ""
    assert "key" in server.choices("collections")[0]["label"].lower()
    assert len(attempts) == 1


def test_collection_refresh_failure_keeps_cached_images_and_backs_off(
    gallery, monkeypatch
):
    server, options, settings, ctx, _, _ = gallery
    assert server.query_preview(options, settings, ctx=ctx)["total"] == 1
    now = server.time.time()
    monkeypatch.setattr(server.time, "time", lambda: now + 86401)
    attempts = []

    def offline(*a, **k):
        attempts.append(True)
        raise TimeoutError("unavailable")

    monkeypatch.setattr(server, "fetch_json", offline)
    stale = server.query_preview(options, settings, ctx=ctx)
    assert stale["total"] == 1 and stale["stale"] is True
    assert "warning" in stale
    assert server.query_preview(options, settings, ctx=ctx)["total"] == 1
    assert len(attempts) == 1
