"""Lineups browse cached eligible artwork and pin without consuming rotation."""

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from flask import Flask


@pytest.fixture
def gallery(monkeypatch, tmp_path):
    folder = Path(__file__).parents[1]
    spec = importlib.util.spec_from_file_location(
        "lineups_civitai", folder / "server.py"
    )
    server = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(server)
    manifest = json.loads((folder / "plugin.json").read_text())
    options = {
        field["name"]: field.get("default") for field in manifest["cell_options"]
    }
    options.update(resolution="native_only", min_reactions=5, min_comments=1)
    settings = {"api_key": "private-test-identity"}
    ctx = {
        "data_dir": str(tmp_path),
        "cell_w": 3840,
        "cell_h": 2160,
        "placement_id": "art",
    }
    app = Flask(__name__)
    app.config["PLUGIN_REGISTRY"] = {
        "picture_civitai": SimpleNamespace(data_dir=tmp_path, manifest=manifest)
    }
    app.config["SETTINGS_STORE"] = SimpleNamespace(
        get_for_runtime=lambda *args: dict(settings)
    )

    def artwork(ident, **changes):
        return {
            "id": ident,
            "url": f"https://image.civitai.com/token/{ident}/original=true/art.png",
            "width": 5000,
            "height": 3000,
            "username": "alice",
            "type": "image",
            "nsfwLevel": "None",
            "browsingLevel": 1,
            "tags": [{"id": 7660, "name": "painting"}],
            "stats": {
                "cryCount": 0,
                "laughCount": 0,
                "heartCount": 0,
                "likeCount": 10,
                "commentCount": 2,
            },
            **changes,
        }

    images = [
        artwork(1),
        artwork(2, width=3000, height=5000),
        artwork(3, width=800, height=600),
        artwork(
            4, tags=[{"id": 7660, "name": "painting"}, {"id": 9, "name": "photography"}]
        ),
        artwork(
            5,
            stats={
                "cryCount": 0,
                "laughCount": 0,
                "heartCount": 0,
                "likeCount": 1,
                "commentCount": 0,
            },
        ),
        artwork(6),
    ]
    calls = []

    def network(url, **kwargs):
        calls.append(url)
        return {"items": images}

    monkeypatch.setattr(server, "fetch_json", network)
    with app.app_context():
        server.fetch(options, settings, ctx={**ctx, "preview": True})
        yield server, options, settings, ctx, calls, app


def test_manifest_declares_pinnable_artworks(gallery):
    *_, app = gallery
    manifest = app.config["PLUGIN_REGISTRY"]["picture_civitai"].manifest
    assert manifest["lineups"] == {"pin_option": "pinned_image", "noun": "artworks"}
    pin = next(
        field for field in manifest["cell_options"] if field["name"] == "pinned_image"
    )
    assert pin["type"] == "string" and pin["default"] == ""


def test_listing_uses_frame_eligibility_and_cached_filters_without_network_or_history(
    gallery, monkeypatch
):
    server, options, _, ctx, calls, _ = gallery
    before = len(calls)
    cache = Path(ctx["data_dir"]) / "civitai-cache.json"
    original = cache.read_bytes()
    monkeypatch.setattr(
        server, "fetch_json", lambda *a, **k: pytest.fail("Listing fetched the network")
    )
    listed = server.lineup_items({**options, "pinned_image": "6"}, ctx=ctx)
    assert [item["key"] for item in listed] == ["1", "6"]
    assert listed[0]["label"] == "alice / 1"
    assert listed[0]["thumb"].startswith("/plugins/picture_civitai/lineup-thumb/")
    assert listed[0]["thumb"].endswith("/320x180.jpg")
    assert server.lineup_items({**options, "min_reactions": 1000}, ctx=ctx) == []
    assert (
        server.lineup_items(options, ctx={**ctx, "cell_w": 2160, "cell_h": 3840})[0][
            "key"
        ]
        == "2"
    )
    assert len(calls) == before and cache.read_bytes() == original
    assert not (cache.parent / "civitai-history.json").exists()


def test_daily_lineup_listing_reuses_render_cache_with_injected_location(gallery):
    server, options, settings, ctx, calls, _ = gallery
    options = {**options, "daily_batch": True}
    server.fetch(
        {**options, "latitude": 40.0, "longitude": -75.0, "label": "Test location"},
        settings,
        ctx={**ctx, "preview": True},
    )
    before = len(calls)
    assert [item["key"] for item in server.lineup_items(options, ctx=ctx)] == ["1", "6"]
    assert len(calls) == before


def test_portrait_lineup_thumbnails_follow_cell_shape_and_exclude_landscape(gallery):
    server, options, _, ctx, _, _ = gallery
    listed = server.lineup_items(options, ctx={**ctx, "cell_w": 2160, "cell_h": 3840})
    assert [item["key"] for item in listed] == ["2"]
    assert listed[0]["thumb"].startswith("/plugins/picture_civitai/lineup-thumb/")
    assert listed[0]["thumb"].endswith("/180x320.jpg")


def test_lineup_thumbnail_is_cropped_once_without_api_requests(gallery, monkeypatch):
    import io

    from PIL import Image

    server, options, _, ctx, calls, app = gallery
    app.register_blueprint(server.blueprint(), url_prefix="/plugins/picture_civitai")
    listed = server.lineup_items(options, ctx={**ctx, "cell_w": 2160, "cell_h": 3840})
    source = io.BytesIO()
    Image.new("RGB", (300, 500), "#638064").save(source, "JPEG")
    downloads = []

    def download(url):
        downloads.append(url)
        return source.getvalue()

    monkeypatch.setattr(server, "_thumbnail_bytes", download)
    before = len(calls)
    client = app.test_client()
    for _ in range(2):
        response = client.get(listed[0]["thumb"])
        assert response.status_code == 200
        with Image.open(io.BytesIO(response.data)) as image:
            assert image.size == (180, 320)
            assert image.getpixel((0, 0)) != (0, 0, 0)
    assert len(downloads) == 1 and len(calls) == before


def test_lineup_thumbnail_rejects_unknown_images_and_unbounded_sizes(gallery):
    server, options, _, ctx, _, app = gallery
    app.register_blueprint(server.blueprint(), url_prefix="/plugins/picture_civitai")
    url = server.lineup_items(options, ctx=ctx)[0]["thumb"]
    client = app.test_client()
    assert client.get(url.replace("320x180", "3200x1800")).status_code == 404
    assert (
        client.get(
            "/plugins/picture_civitai/lineup-thumb/unknown/320x180.jpg"
        ).status_code
        == 404
    )


def test_listing_uses_manifest_defaults_and_isolates_credentials(gallery):
    server, options, settings, ctx, _, _ = gallery
    minimal = {
        name: options[name] for name in ("resolution", "min_reactions", "min_comments")
    }
    assert [item["key"] for item in server.lineup_items(minimal, ctx=ctx)] == ["1", "6"]
    settings["api_key"] = "different-identity"
    assert server.lineup_items(options, ctx=ctx) == []


def test_listing_is_cache_only_on_miss_stale_pool_and_older_hosts(
    gallery, monkeypatch, tmp_path
):
    server, options, _, ctx, calls, _ = gallery
    before = len(calls)
    assert server.lineup_items(options) == []
    assert (
        server.lineup_items(options, ctx={**ctx, "data_dir": str(tmp_path / "empty")})
        == []
    )
    assert server.lineup_items({**options, "orientation": "landscape"}) == []
    assert [
        item["key"]
        for item in server.lineup_items(
            {**options, "orientation": "landscape", "resolution": "allow_smaller"}
        )
    ] == ["1", "3", "6"]
    now = server.time.time()
    monkeypatch.setattr(server.time, "time", lambda: now + 86401)
    assert [item["key"] for item in server.lineup_items(options, ctx=ctx)] == ["1", "6"]
    assert len(calls) == before


def test_pin_renders_exact_item_and_preserves_history_then_unpin_rotates(
    gallery, monkeypatch
):
    server, options, settings, ctx, _, _ = gallery
    now = [server.time.time()]
    monkeypatch.setattr(server.time, "time", lambda: now[0])
    first = server.fetch(options, settings, ctx=ctx)
    target = "6" if first["selected_id"] == 1 else "1"
    history = Path(ctx["data_dir"]) / "civitai-history.json"
    original = history.read_bytes()
    pinned = server.fetch({**options, "pinned_image": target}, settings, ctx=ctx)
    assert [str(image["id"]) for image in pinned["images"]] == [target]
    assert str(pinned["selected_id"]) == target
    assert history.read_bytes() == original
    now[0] += 3600
    resumed = server.fetch({**options, "pinned_image": ""}, settings, ctx=ctx)
    assert resumed["selected_id"] != first["selected_id"]


def test_query_preview_keeps_browsing_collection_while_an_artwork_is_pinned(gallery):
    server, options, settings, ctx, _, _ = gallery
    result = server.query_preview({**options, "pinned_image": "1"}, settings, ctx=ctx)
    assert {str(image["id"]) for image in result["images"]} == {"1", "6"}
    assert not (Path(ctx["data_dir"]) / "civitai-history.json").exists()


def test_lexica_lists_small_previews_and_pins_stable_provider_keys(
    gallery, monkeypatch
):
    server, options, settings, ctx, _, _ = gallery
    monkeypatch.setattr(server, "LEXICA_ENABLED", True)
    ident = "0482ee68-0368-4eca-8846-5930db866b33"
    small = f"https://image.lexica.art/sm/{ident}"
    monkeypatch.setattr(
        server,
        "fetch_json",
        lambda *a, **k: {
            "images": [
                {
                    "id": ident,
                    "src": f"https://image.lexica.art/md/{ident}",
                    "srcSmall": small,
                    "width": 2400,
                    "height": 1600,
                    "prompt": "A cyberpunk painting",
                    "nsfw": False,
                    "grid": False,
                }
            ]
        },
    )
    options = {
        **options,
        "provider": "lexica",
        "lexica_query": "cyberpunk",
        "resolution": "allow_smaller",
    }
    server.fetch(options, settings, ctx={**ctx, "preview": True})
    monkeypatch.setattr(
        server, "fetch_json", lambda *a, **k: pytest.fail("Listing fetched the network")
    )
    listed = server.lineup_items(options, ctx=ctx)
    assert listed == [
        {
            "key": f"lexica:{ident}",
            "label": "Lexica / A cyberpunk painting",
            "thumb": small,
        }
    ]
    pinned = server.fetch(
        {**options, "pinned_image": listed[0]["key"]}, settings, ctx=ctx
    )
    assert pinned["selected_id"] == listed[0]["key"]
    assert len(pinned["images"]) == 1
    assert not (Path(ctx["data_dir"]) / "civitai-history.json").exists()


@pytest.mark.parametrize("key", ["2", "3", "4", "5", "999", "../../escape"])
def test_pin_never_substitutes_another_image_or_bypasses_filters(gallery, key):
    server, options, settings, ctx, _, _ = gallery
    result = server.fetch({**options, "pinned_image": key}, settings, ctx=ctx)
    assert result["images"] == [] and "pinned" in result["error"].lower()
    assert not (Path(ctx["data_dir"]) / "civitai-history.json").exists()


def test_combined_galleries_list_and_pin_from_cache_without_changing_history(
    gallery, monkeypatch
):
    server, options, settings, ctx, _, _ = gallery
    monkeypatch.setattr(server, "LEXICA_ENABLED", True)
    ident = "0482ee68-0368-4eca-8846-5930db866b33"
    monkeypatch.setattr(
        server,
        "fetch_json",
        lambda *a, **k: {
            "images": [
                {
                    "id": ident,
                    "src": f"https://image.lexica.art/md/{ident}",
                    "srcSmall": f"https://image.lexica.art/sm/{ident}",
                    "width": 5000,
                    "height": 3000,
                    "prompt": "A cyberpunk painting",
                    "nsfw": False,
                    "grid": False,
                }
            ]
        },
    )
    options = {**options, "provider": "both", "lexica_query": "city"}
    server.fetch(options, settings, ctx={**ctx, "preview": True})
    monkeypatch.setattr(
        server, "fetch_json", lambda *a, **k: pytest.fail("Listing fetched the network")
    )
    listed = server.lineup_items(options, ctx=ctx)
    assert {item["key"] for item in listed} == {"1", "6", f"lexica:{ident}"}
    pinned = server.fetch(
        {**options, "pinned_image": f"lexica:{ident}"}, settings, ctx=ctx
    )
    assert pinned["selected_id"] == f"lexica:{ident}"
    assert len(pinned["images"]) == 1
    assert not (Path(ctx["data_dir"]) / "civitai-history.json").exists()


def test_thumbnail_download_sends_a_user_agent(monkeypatch):
    """Civitai's image CDN answers 403 to urllib's default User-Agent, which left every lineup
    preview as a "?" placeholder."""
    folder = Path(__file__).parents[1]
    spec = importlib.util.spec_from_file_location("lineups_civitai_ua", folder / "server.py")
    server = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(server)
    seen = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self, n):
            return b"jpeg"

    class Opener:
        def open(self, request, timeout):
            seen["ua"] = request.get_header("User-agent")
            return Response()

    monkeypatch.setattr(server.urllib.request, "build_opener", lambda *handlers: Opener())
    assert server._thumbnail_bytes("https://image.civitai.com/x/abc/width=320/abc.jpeg") == b"jpeg"
    assert seen["ua"] and "Python-urllib" not in seen["ua"]
