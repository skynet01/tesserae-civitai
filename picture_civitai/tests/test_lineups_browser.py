"""The real lineup editor discovers cached artworks for a portrait cell."""

import io
import os
import shutil
import threading
from pathlib import Path

import pytest


def test_cached_daily_artworks_show_arrows_and_portrait_previews(tmp_path, monkeypatch):
    from PIL import Image, ImageDraw
    from playwright.sync_api import expect, sync_playwright
    from werkzeug.serving import make_server

    from app.composer import page_preview_token
    from app.main import REPO_ROOT, create_app
    from app.state.deck_model import Deck, DeckPage
    from app.state.page_store import Cell, Page, Panel

    with sync_playwright() as playwright:
        executable = os.environ.get(
            "PLAYWRIGHT_CHROMIUM_EXECUTABLE", playwright.chromium.executable_path
        )
        if not Path(executable).exists():
            pytest.skip("Playwright Chromium not installed")
        plugins = tmp_path / "plugins"
        plugins.mkdir()
        for plugin in (REPO_ROOT / "plugins").iterdir():
            (plugins / plugin.name).symlink_to(plugin)
        shutil.copytree(Path(__file__).parents[1], plugins / "picture_civitai")
        app = create_app(
            testing=True,
            data_root=tmp_path / "data",
            plugins_dir=plugins,
            devices_dir=REPO_ROOT / "devices",
            renderers_dir=REPO_ROOT / "renderers",
        )
        settings = app.config["SETTINGS_STORE"]
        settings.patch_section(
            "app",
            {
                "onboarded": True,
                "latitude": 40.0,
                "longitude": -75.0,
                "location_name": "Test location",
            },
        )
        settings.patch_section("auth", {"disabled": True})
        client = app.test_client()
        code = app.config["PAIRING_STORE"].issue(note="test").code
        assert (
            client.post(
                "/api/v1/device/register",
                headers={"X-Pairing-Code": code},
                json={"device_id": "test_frame", "kind": "esp32_client"},
            ).status_code
            == 201
        )
        app.config["DEVICE_REGISTRY"].get("test_frame").manifest["panel"] = {
            "w": 600,
            "h": 1000,
            "orientation": "portrait",
            "gamut": "srgb",
        }
        plugin = app.config["PLUGIN_REGISTRY"].get("picture_civitai")
        module = plugin.server_module
        options = {
            **plugin.cell_option_defaults(),
            "positive_tags": "",
            "negative_tags": "",
            "resolution": "allow_smaller",
            "daily_batch": True,
            "show_credit": False,
        }
        calls = []

        def network(url, **kwargs):
            calls.append(url)
            return {
                "items": [
                    {
                        "id": ident,
                        "url": f"https://image.civitai.com/test/{ident}/original=true/art.jpg",
                        "width": w,
                        "height": h,
                        "type": "image",
                        "nsfwLevel": "None",
                        "browsingLevel": 1,
                        "username": "artist",
                        "tags": [],
                    }
                    for ident, w, h in (
                        (101, 600, 1000),
                        (102, 720, 1200),
                        (103, 1000, 600),
                    )
                ]
            }

        monkeypatch.setattr(module, "fetch_json", network)
        with app.app_context():
            module.fetch(
                {
                    **options,
                    "latitude": 40.0,
                    "longitude": -75.0,
                    "label": "Test location",
                },
                {},
                ctx={
                    "data_dir": str(plugin.data_dir),
                    "cell_w": 600,
                    "cell_h": 1000,
                    "placement_id": "art",
                    "preview": True,
                },
            )
        before = len(calls)
        image = Image.new("RGB", (300, 500), "#d9c9a9")
        draw = ImageDraw.Draw(image)
        draw.ellipse((55, 65, 180, 190), fill="#b77743")
        draw.polygon(
            ((0, 340), (180, 205), (300, 375), (300, 500), (0, 500)), fill="#47716b"
        )
        raw = io.BytesIO()
        image.save(raw, "JPEG")
        monkeypatch.setattr(module, "_thumbnail_bytes", lambda url: raw.getvalue())
        dashboard = Page(
            id="art",
            name="Civitai artwork",
            device_ids=["test_frame"],
            panel=Panel(w=600, h=1000),
            cells=[
                Cell(
                    id="art",
                    plugin="picture_civitai",
                    x=0,
                    y=0,
                    w=600,
                    h=1000,
                    options=options,
                )
            ],
        )
        app.config["PAGE_STORE"].save(dashboard)
        app.config["DECK_STORE"].upsert(
            Deck(
                id="gallery",
                name="Artwork rotation",
                device_ids=["test_frame"],
                pages=[DeckPage(page_id="art")],
            )
        )
        cache = tmp_path / "data/core/previews"
        cache.mkdir(parents=True, exist_ok=True)
        image.save(cache / f"art__{page_preview_token(dashboard, (600, 1000))}.png")
        listing = client.get(
            "/decks/dashboard-items?page_id=art&display_id=test_frame"
        ).json
        assert listing["total"] == 2 and listing["pinnable"]
        for entry in listing["items"]:
            response = client.get(entry["thumb_url"])
            assert response.status_code == 200
            with Image.open(io.BytesIO(response.data)) as thumbnail:
                assert thumbnail.size == (192, 320)
        server = make_server("127.0.0.1", 0, app, threaded=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        browser = playwright.chromium.launch(executable_path=executable)
        try:
            page = browser.new_page(viewport={"width": 1440, "height": 1000})
            page.goto(
                f"http://127.0.0.1:{server.server_port}/decks/gallery/edit",
                wait_until="load",
            )
            tile = page.locator('[data-step="art"] .lu-tile')
            tile.hover()
            expect(tile.locator(".lu-count")).to_have_text("×2")
            expect(
                tile.get_by_role("button", name="Previous artwork", exact=True)
            ).to_be_visible()
            expect(
                tile.get_by_role("button", name="Next artwork", exact=True)
            ).to_be_visible()
            screenshot = os.environ.get("CIVITAI_ITEMS_SCREENSHOT")
            if screenshot:
                Path(screenshot).parent.mkdir(parents=True, exist_ok=True)
                page.screenshot(path=screenshot, full_page=True)
            assert len(calls) == before
        finally:
            browser.close()
            server.shutdown()
            thread.join(timeout=5)
