"""Source and collection checkboxes in the real canvas configuration drawer."""

import os
import shutil
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest


@pytest.fixture
def browser():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        executable = os.environ.get(
            "PLAYWRIGHT_CHROMIUM_EXECUTABLE", playwright.chromium.executable_path
        )
        if not Path(executable).exists():
            pytest.skip("Playwright Chromium not installed")
        browser = playwright.chromium.launch(executable_path=executable)
        yield browser
        browser.close()


@pytest.mark.parametrize(
    "width, collection_id, label",
    [
        (1440, "18707243", "Cool art (yours)"),
        (390, "55", "Landscapes (followed, by alice)"),
    ],
)
def test_choose_own_or_followed_collection_and_preview_without_tag_or_creator_search(
    browser, monkeypatch, tmp_path, width, collection_id, label
):
    from app.main import create_app

    root = tmp_path / "plugins"
    shutil.copytree(Path(__file__).parents[1], root / "picture_civitai")
    app = create_app(testing=False, data_root=tmp_path / "data", plugins_dir=root)
    app.config["TESTING"] = True
    plugin = app.config["PLUGIN_REGISTRY"].get("picture_civitai")
    app.config["SETTINGS_STORE"].update_for_namespace(
        "plugins",
        "picture_civitai",
        {"api_key": "test-collections-key"},
        plugin.manifest["settings"],
    )
    calls = []

    def network(url, **kwargs):
        calls.append(urlsplit(url).path)
        if "collection.getAllUser" in url:
            data = [
                {"id": 18707243, "name": "Cool art", "type": "Image", "isOwner": True},
                {
                    "id": 55,
                    "name": "Landscapes",
                    "type": "Image",
                    "isOwner": False,
                    "ownerUsername": "alice",
                },
            ]
        elif "image.getInfinite" in url:
            data = {
                "items": [
                    {
                        "id": 101,
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
                    }
                ]
            }
        else:
            pytest.fail("Disabled tags or creators made a request")
        return {"result": {"data": {"json": data}}}

    monkeypatch.setattr(plugin.server_module, "fetch_json", network)
    client = app.test_client()
    client.post("/setup", data={"password": "abcdefgh", "password_confirm": "abcdefgh"})
    location = client.post("/pages/canvas/new").location
    cid = location.rsplit("/", 1)[-1]
    response = client.post(
        f"/pages/canvas/c/{cid}/save",
        json={
            "els": [
                {
                    "id": "art",
                    "kind": "widget",
                    "widget": "picture_civitai",
                    "x": 0,
                    "y": 0,
                    "w": 900,
                    "h": 600,
                    "options": {
                        "provider": "civitai",
                        "sources": ["tags", "creators"],
                        "resolution": "allow_smaller",
                    },
                }
            ]
        },
    )
    assert response.status_code == 200
    page = browser.new_page(viewport={"width": width, "height": 1000})
    page.set_default_timeout(6000)
    posts = []

    def serve(route):
        request = route.request
        parsed = urlsplit(request.url)
        path = parsed.path + (f"?{parsed.query}" if parsed.query else "")
        if parsed.path.endswith("/stream"):
            route.fulfill(body="", content_type="text/event-stream")
            return
        if parsed.path.endswith("/data.json"):
            route.fulfill(body='{"images":[]}', content_type="application/json")
            return
        if parsed.path.endswith("/query-preview"):
            posts.append(parse_qs(request.post_data))
        response = client.open(
            path,
            method=request.method,
            data=request.post_data_buffer,
            content_type=request.headers.get("content-type"),
        )
        route.fulfill(
            status=response.status_code,
            body=response.get_data(),
            headers={"Content-Type": response.content_type},
        )

    page.route("http://preview.test/**", serve)
    page.route(
        "https://image.civitai.com/**",
        lambda route: route.fulfill(
            content_type="image/svg+xml",
            body='<svg xmlns="http://www.w3.org/2000/svg" width="400" height="250"><rect width="400" height="250" fill="#6e847b"/></svg>',
        ),
    )
    page.goto(f"http://preview.test{location}")
    page.locator('#panels-artboard [data-id="art"]').click()
    page.get_by_role("button", name="Configure").click()
    page.get_by_role("checkbox", name="My and followed collections", exact=True).check()
    page.get_by_role("checkbox", name="Tags", exact=True).uncheck()
    page.get_by_role("checkbox", name="Creators", exact=True).uncheck()
    page.get_by_role("checkbox", name=label, exact=True).check()
    assert page.get_by_role(
        "checkbox", name="Landscapes (followed, by alice)", exact=True
    ).is_visible()
    assert page.locator('[name="opt_users"]').is_hidden()
    assert page.locator("body").evaluate("el => el.scrollWidth <= innerWidth")
    page.screenshot(path=str(tmp_path / f"collection-picker-{width}.png"))
    page.get_by_role("button", name="Preview query", exact=True).click()
    page.wait_for_function(
        "document.querySelectorAll('.query-preview-item').length === 1"
    )
    assert posts[0]["opt_sources"] == ["collections"]
    assert posts[0]["opt_collections"] == [collection_id]
    assert f"Collection: {label}" in page.locator(".query-preview-badges").inner_text()
    assert calls == ["/api/trpc/collection.getAllUser", "/api/trpc/image.getInfinite"]
    page.close()
