"""Query previews share display eligibility without consuming rotation."""

import importlib.util
from pathlib import Path

import pytest


@pytest.fixture
def server():
    spec = importlib.util.spec_from_file_location(
        "preview_server", Path(__file__).parents[1] / "server.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def lexica_enabled(server, monkeypatch):
    monkeypatch.setattr(server, "LEXICA_ENABLED", True)


def civitai(ident, **extra):
    return dict(
        id=ident,
        url=f"https://image.civitai.com/a/{ident}.jpg",
        width=2400,
        height=1600,
        username="alice",
        tags=[{"id": 7660, "name": "painting"}],
        **extra,
    )


def test_preview_caps_eligible_matches_and_explains_them_without_history(
    server, monkeypatch, tmp_path
):
    pool = [civitai(i) for i in range(1, 30)]
    pool[0] = {**pool[0], "width": 800, "height": 1200}
    monkeypatch.setattr(
        server,
        "_fetch_pool",
        lambda *a, **k: {"images": pool, "warning": "Sampled tags"},
    )
    options = {"positive_tags": "painting, 7660", "users": "alice", "min_reactions": 0}
    result = server.query_preview(
        options,
        {},
        ctx={
            "data_dir": str(tmp_path),
            "cell_w": 1600,
            "cell_h": 900,
            "placement_id": "test",
        },
    )
    assert len(result["images"]) == 20
    assert result["total"] == 28
    assert result["images"][0]["id"] == 2
    assert result["images"][0]["matches"] == [
        "Creator: alice",
        "Tag: 7660",
        "Tag: painting",
    ]
    assert result["warning"] == "Sampled tags"
    assert not (tmp_path / "civitai-history.json").exists()


def lexica(ident="0482ee68-0368-4eca-8846-5930db866b33", **extra):
    return dict(
        id=ident,
        src=f"https://image.lexica.art/md/{ident}",
        srcSmall=f"https://image.lexica.art/sm/{ident}",
        width=2400,
        height=1600,
        prompt="A cyberpunk city painting",
        nsfw=False,
        grid=False,
        **extra,
    )


def test_lexica_search_filters_prompt_and_reuses_cache_without_civitai_key(
    server, monkeypatch, tmp_path, lexica_enabled
):
    calls = []

    def network(url, **kwargs):
        calls.append((url, kwargs))
        return {
            "images": [
                lexica(),
                {
                    **lexica(),
                    "id": "1482ee68-0368-4eca-8846-5930db866b33",
                    "prompt": "A photographic city",
                },
                {**lexica(), "nsfw": True},
            ]
        }

    monkeypatch.setattr(server, "fetch_json", network)
    opts = {
        "provider": "lexica",
        "lexica_query": "cyberpunk city",
        "positive_tags": "cyberpunk",
        "negative_tags": "photographic",
        "users": "ignored",
        "min_reactions": 100,
    }
    ctx = {
        "data_dir": str(tmp_path),
        "cell_w": 1600,
        "cell_h": 900,
        "placement_id": "p",
        "preview": True,
    }
    result = server.query_preview(
        opts, {"api_key_secret": "must-stay-at-civitai"}, ctx=ctx
    )
    assert len(result["images"]) == 1
    assert result["images"][0]["matches"] == [
        "Search: cyberpunk city",
        "Prompt: cyberpunk",
    ]
    assert result["images"][0]["provider"] == "lexica"
    assert result["images"][0]["id"].startswith("lexica:")
    server.query_preview(opts, {}, ctx=ctx)
    assert len(calls) == 1
    assert calls[0][0] == "https://lexica.art/api/v1/search?q=cyberpunk+city"
    assert "Authorization" not in calls[0][1].get("headers", {})
    assert not (tmp_path / "civitai-history.json").exists()


@pytest.mark.parametrize(
    "extra",
    [
        {"nsfw": None},
        {"nsfw": "false"},
        {"grid": True},
        {"src": "https://evil.test/a"},
        {"id": "../bad"},
        {"width": 0},
    ],
)
def test_lexica_rejects_unsafe_or_unclassified_results(
    server, monkeypatch, tmp_path, extra, lexica_enabled
):
    monkeypatch.setattr(
        server, "fetch_json", lambda *a, **k: {"images": [{**lexica(), **extra}]}
    )
    assert (
        server.fetch(
            {"provider": "lexica", "lexica_query": "city"},
            {},
            ctx={"data_dir": str(tmp_path)},
        )["images"]
        == []
    )


def test_lexica_history_survives_reload(server, monkeypatch, tmp_path, lexica_enabled):
    monkeypatch.setattr(server, "fetch_json", lambda *a, **k: {"images": [lexica()]})
    ctx = {
        "data_dir": str(tmp_path),
        "placement_id": "p",
        "cell_w": 1600,
        "cell_h": 900,
    }
    result = server.fetch({"provider": "lexica", "lexica_query": "city"}, {}, ctx=ctx)
    assert result["selected_id"].startswith("lexica:")
    records = server._read_history(tmp_path / "civitai-history.json")
    assert next(iter(records.values()))["selected"] == result["selected_id"]


def test_unsupported_provider_does_not_fetch(server, monkeypatch, tmp_path):
    monkeypatch.setattr(
        server, "fetch_json", lambda *a, **k: pytest.fail("Unexpected network")
    )
    result = server.fetch(
        {"provider": "arbitrary"}, {}, ctx={"data_dir": str(tmp_path)}
    )
    assert result["images"] == []
    assert "service" in result["error"].lower()


@pytest.mark.parametrize("provider", ["lexica", "both"])
def test_saved_lexica_selection_previews_civitai_only(
    server, monkeypatch, tmp_path, provider
):
    def network(url, **kwargs):
        assert url.startswith(server.API_URL)
        return {"items": [civitai(1, type="image", nsfwLevel="None", browsingLevel=1)]}

    monkeypatch.setattr(server, "fetch_json", network)
    result = server.query_preview(
        {
            "provider": provider,
            "lexica_query": "city",
            "users": "alice",
            "positive_tags": "painting",
        },
        {},
        ctx={"data_dir": str(tmp_path), "cell_w": 1600, "cell_h": 900},
    )
    assert [image["id"] for image in result["images"]] == [1]
    assert result["images"][0]["matches"] == ["Creator: alice", "Tag: painting"]


def test_paused_lexica_pin_has_an_actionable_error(server, monkeypatch, tmp_path):
    monkeypatch.setattr(
        server,
        "fetch_json",
        lambda *a, **k: pytest.fail("Pinned Lexica artwork fetched the network"),
    )
    result = server.fetch(
        {"provider": "both", "pinned_image": f"lexica:{lexica()['id']}"},
        {},
        ctx={"data_dir": str(tmp_path)},
    )
    assert result["images"] == []
    assert "Clear the pin" in result["error"]


def test_combined_galleries_merge_and_reuse_independent_caches(
    server, monkeypatch, tmp_path, lexica_enabled
):
    calls = []

    def network(url, **kwargs):
        calls.append((url, kwargs))
        if url.startswith(server.LEXICA_URL):
            return {"images": [lexica()]}
        return {
            "items": [
                {
                    **civitai(1),
                    "type": "image",
                    "nsfwLevel": "None",
                    "browsingLevel": 1,
                    "stats": {
                        "cryCount": 0,
                        "laughCount": 0,
                        "likeCount": 10,
                        "heartCount": 0,
                        "commentCount": 3,
                    },
                }
            ]
        }

    monkeypatch.setattr(server, "fetch_json", network)
    options = {
        "provider": "both",
        "lexica_query": "city",
        "positive_tags": "painting",
        "negative_tags": "photo",
        "users": "alice",
        "min_reactions": 5,
        "min_comments": 2,
        "resolution": "allow_smaller",
    }
    ctx = {"data_dir": str(tmp_path), "cell_w": 1600, "cell_h": 900, "preview": True}
    settings = {"api_key": "private-civitai-only"}
    result = server.query_preview(options, settings, ctx=ctx)
    assert {str(image["id"]) for image in result["images"]} == {
        "1",
        f"lexica:{lexica()['id']}",
    }
    assert result["total"] == 2
    assert result["images"][0]["matches"][0] == "Creator: alice"
    assert len(calls) == 3
    assert "Authorization" not in next(
        kwargs["headers"] for url, kwargs in calls if url.startswith(server.LEXICA_URL)
    )
    server.fetch(options, settings, ctx=ctx)
    server.fetch({**options, "provider": "civitai"}, settings, ctx=ctx)
    server.fetch({**options, "provider": "lexica"}, settings, ctx=ctx)
    assert len(calls) == 3
    assert not (tmp_path / "civitai-history.json").exists()


@pytest.mark.parametrize("failed", ["civitai", "lexica", "both"])
def test_combined_galleries_keep_working_when_either_service_fails(
    server, monkeypatch, tmp_path, failed, lexica_enabled
):
    def network(url, **kwargs):
        service = "lexica" if url.startswith(server.LEXICA_URL) else "civitai"
        if failed in (service, "both"):
            raise TimeoutError("service unavailable")
        if service == "lexica":
            return {"images": [lexica()]}
        return {
            "items": [
                {**civitai(1), "type": "image", "nsfwLevel": "None", "browsingLevel": 1}
            ]
        }

    monkeypatch.setattr(server, "fetch_json", network)
    result = server.fetch(
        {"provider": "both", "lexica_query": "city"},
        {},
        ctx={"data_dir": str(tmp_path), "preview": True},
    )
    if failed == "both":
        assert (
            result["images"] == []
            and "Civitai" in result["error"]
            and "Lexica" in result["error"]
        )
    else:
        assert len(result["images"]) == 1
        assert "error" not in result
        assert ("Civitai" if failed == "civitai" else "Lexica") in result["warning"]


def test_combined_collection_cap_keeps_both_services_and_native_rules(
    server, monkeypatch, tmp_path, lexica_enabled
):
    def network(url, **kwargs):
        if url.startswith(server.LEXICA_URL):
            return {
                "images": [
                    lexica(f"{i:08x}-0368-4eca-8846-5930db866b33") for i in range(50)
                ]
            }
        return {
            "items": [
                {**civitai(i), "type": "image", "nsfwLevel": "None", "browsingLevel": 1}
                for i in range(1, 101)
            ]
        }

    monkeypatch.setattr(server, "fetch_json", network)
    options = {"provider": "both", "lexica_query": "city"}
    ctx = {"data_dir": str(tmp_path), "preview": True, "cell_w": 1600, "cell_h": 900}
    result = server.fetch(options, {}, ctx=ctx)
    assert len(result["images"]) == server.MAX_IMAGES
    assert sum(image.get("provider") == "lexica" for image in result["images"]) == 50
    assert (
        server.query_preview(
            {**options, "resolution": "native_only"},
            {},
            ctx={**ctx, "cell_w": 3840, "cell_h": 2160},
        )["total"]
        == 0
    )


def test_lexica_failure_keeps_cached_matches_and_backs_off(
    server, monkeypatch, tmp_path, lexica_enabled
):
    from urllib.error import HTTPError

    now = [100000]
    monkeypatch.setattr(server.time, "time", lambda: now[0])
    calls = []
    errors = []

    def network(url, **kwargs):
        calls.append(url)
        if len(calls) == 1:
            return {"images": [lexica()]}
        error = HTTPError(url, 429, "Limited", {"Retry-After": "120"}, None)
        real_close = error.close

        def close():
            errors.append("closed")
            real_close()

        error.close = close
        raise error

    monkeypatch.setattr(server, "fetch_json", network)
    options = {"provider": "lexica", "lexica_query": "city", "refresh_hours": 1}
    ctx = {"data_dir": str(tmp_path), "preview": True}
    assert len(server.fetch(options, {}, ctx=ctx)["images"]) == 1
    now[0] += 3601
    stale = server.fetch(options, {}, ctx=ctx)
    assert len(stale["images"]) == 1 and stale["stale"] is True
    assert errors == ["closed"]
    server.fetch({**options, "lexica_query": "different search"}, {}, ctx=ctx)
    assert len(calls) == 2


def test_civitai_credentials_cannot_redirect_to_lexica(server):
    from urllib.request import Request

    request = Request(server.API_URL, headers={"Authorization": "Bearer private"})
    with pytest.raises(server.FeedError):
        server._ApiRedirect().redirect_request(
            request, None, 302, "Found", {}, server.LEXICA_URL
        )


def test_real_widget_preview_through_host_uses_unsaved_filters(monkeypatch, tmp_path):
    import shutil

    from app.main import create_app

    plugins = tmp_path / "plugins"
    shutil.copytree(Path(__file__).parents[1], plugins / "picture_civitai")
    app = create_app(testing=False, data_root=tmp_path / "data", plugins_dir=plugins)
    app.config["TESTING"] = True
    if not any(
        rule.rule == "/plugins/<plugin_id>/query-preview" and "POST" in rule.methods
        for rule in app.url_map.iter_rules()
    ):
        pytest.skip("Host does not provide the optional query-preview hook")
    plugin = app.config["PLUGIN_REGISTRY"].get("picture_civitai")
    assert plugin is not None

    def network(url, **kwargs):
        return {
            "items": [
                {
                    **civitai(i),
                    "url": f"https://image.civitai.com/a/{i}/width=2400/test.jpg",
                    "type": "image",
                    "nsfwLevel": "None",
                    "browsingLevel": 1,
                }
                for i in range(1, 30)
            ]
        }

    monkeypatch.setattr(plugin.server_module, "fetch_json", network)
    client = app.test_client()
    client.post("/setup", data={"password": "abcdefgh", "password_confirm": "abcdefgh"})
    response = client.post(
        "/plugins/picture_civitai/query-preview",
        data={
            "opt_provider": "civitai",
            "opt_sources": ["tags", "creators"],
            "opt_positive_tags": "painting",
            "opt_negative_tags": "photography",
            "opt_users": "",
            "opt_orientation": "auto",
            "opt_resolution": "allow_smaller",
            "cell_w": 3840,
            "cell_h": 2160,
        },
    )
    assert response.status_code == 200
    result = response.get_json()
    assert result["total"] == 29
    assert len(result["images"]) == 20
    assert result["images"][0]["matches"] == ["Tag: painting"]
    assert "/width=480/" in result["images"][0]["thumbnail"]
    assert not (plugin.data_dir / "civitai-history.json").exists()
