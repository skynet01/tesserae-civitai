"""Durable placement rotation tests; candidate acquisition stays offline."""

import importlib.util
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest


def load_server():
    path = Path(__file__).parents[1] / "server.py"
    spec = importlib.util.spec_from_file_location("civitai_rotation_server", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def rotation(monkeypatch, tmp_path):
    server = load_server()
    clock = [1_800_000_000.0]
    monkeypatch.setattr(server.time, "time", lambda: clock[0])
    ctx = {
        "data_dir": str(tmp_path),
        "placement_id": "page/cell@frame",
        "cell_w": 1600,
        "cell_h": 1200,
        "preview": False,
        "settled": True,
    }

    def choose(images, options=None, context=None, metadata=None, module=None):
        active = module or server
        monkeypatch.setattr(
            active,
            "_fetch_pool",
            lambda *args, **kwargs: {
                "images": images,
                **(metadata or {}),
            },
        )
        return active.fetch(options or {}, {}, ctx=context or ctx)

    return server, clock, ctx, choose


def image(ident, *, width=2400, height=1600, priority=1, stats=None):
    return {
        "id": ident,
        "url": f"https://image.civitai.com/x/{ident}.jpeg",
        "width": width,
        "height": height,
        "username": "artist",
        "source_priority": priority,
        "tags": [],
        **({"stats": stats} if stats is not None else {}),
    }


def test_bucket_is_stable_after_pool_reordering_and_another_fetch(rotation):
    _, clock, _, choose = rotation
    images = [image(1), image(2), image(3)]
    selected = choose(images)["selected_id"]
    clock[0] += 1
    assert choose(list(reversed(images)))["selected_id"] == selected
    clock[0] += 3600
    assert choose(images)["selected_id"] != selected


def test_creators_are_first_but_an_unseen_tag_beats_a_recent_creator(rotation):
    _, clock, _, choose = rotation
    images = [image(1, priority=0), image(2), image(3)]
    assert choose(images)["selected_id"] == 1
    clock[0] += 3600
    assert choose(images)["selected_id"] in (2, 3)


def test_all_recent_images_reuse_the_oldest_even_when_a_newer_creator_exists(rotation):
    _, clock, _, choose = rotation
    assert choose([image(1)])["selected_id"] == 1
    clock[0] += 3600
    assert choose([image(1), image(2, priority=0)])["selected_id"] == 2
    clock[0] += 3600
    assert choose([image(1), image(2, priority=0)])["selected_id"] == 1


def test_history_survives_module_reload_and_collection_replacement(rotation):
    _, clock, _, choose = rotation
    assert choose([image(1, priority=0)])["selected_id"] == 1
    clock[0] += 86400
    fresh = load_server()
    assert choose([image(1, priority=0), image(99)], module=fresh)["selected_id"] == 99


@pytest.mark.parametrize("same_post", [True, False])
def test_recent_civitai_batch_is_deferred_across_collection_replacement(rotation, same_post):
    _, clock, _, choose = rotation
    posted = 1_790_000_000
    first = {**image(1), "postId": 10, "created_at": posted}
    similar = {
        **image(2, priority=0),
        "postId": 10 if same_post else 11,
        "created_at": posted + (86400 if same_post else 120),
    }
    different = {**image(3), "username": "other_artist", "created_at": posted}
    assert choose([first])["selected_id"] == 1
    clock[0] += 3600
    assert choose([similar, different], module=load_server())["selected_id"] == 3
    clock[0] += 3600
    assert choose([similar, different])["selected_id"] == 2


def test_fresh_art_favors_new_eligible_images_without_immediate_repeats(rotation):
    _, clock, _, choose = rotation
    older = {**image(2), "created_at": 1_790_000_000}
    newer = {**image(1), "created_at": 1_790_010_000}
    options = {"prioritize_fresh": True}
    assert choose([older, newer], options)["selected_id"] == 1
    clock[0] += 3600
    assert choose([older, newer], options)["selected_id"] == 2


@pytest.mark.parametrize("changes", [{"preview": True}, {"settled": False}])
def test_previews_and_unsettled_renders_do_not_advance_history(rotation, changes):
    _, clock, ctx, choose = rotation
    images = [image(1, priority=0), image(2)]
    assert choose(images)["selected_id"] == 1
    path = Path(ctx["data_dir"]) / "civitai-history.json"
    original = path.read_bytes()
    clock[0] += 3600
    assert choose(images, context={**ctx, **changes})["selected_id"] == 2
    assert path.read_bytes() == original
    assert choose(images)["selected_id"] == 2


def test_repeat_window_zero_disables_lookback_but_keeps_a_stable_bucket(rotation):
    _, clock, _, choose = rotation
    images = [image(1, priority=0), image(2)]
    assert choose(images, {"repeat_days": 0})["selected_id"] == 1
    clock[0] += 3600
    assert choose(images, {"repeat_days": 0})["selected_id"] == 1
    assert choose(list(reversed(images)), {"repeat_days": 0})["selected_id"] == 1


def test_expired_creator_history_beats_a_more_recent_tag(rotation):
    _, clock, _, choose = rotation
    assert choose([image(1, priority=0)])["selected_id"] == 1
    clock[0] += 86400
    assert choose([image(1, priority=0), image(2)])["selected_id"] == 2
    clock[0] += 86400
    assert (
        choose([image(1, priority=0), image(2)], {"repeat_days": 2})["selected_id"] == 1
    )


def test_history_never_relaxes_orientation_or_native_preference(rotation):
    _, clock, ctx, choose = rotation
    images = [
        image(1),
        image(2, width=800, height=600),
        image(3, width=1800, height=2400),
    ]
    assert choose(images)["selected_id"] == 1
    clock[0] += 3600
    assert choose(images)["selected_id"] == 1
    assert "selected_id" not in choose(images[1:], {"resolution": "native_only"})
    square = {**ctx, "cell_w": 800, "cell_h": 800}
    assert (
        choose([*images, image(4, width=1000, height=1000)], context=square)[
            "selected_id"
        ]
        == 4
    )
    # Physical dimensions are supplied by the server context, not a virtual
    # browser cell reduced by content zoom.
    assert "selected_id" not in choose(
        [image(2, width=800, height=600)], {"resolution": "native_only"}
    )


def test_fit_can_use_an_image_that_fill_would_need_to_enlarge(rotation):
    _, _, _, choose = rotation
    images = [image(1, width=1800, height=1000)]
    assert (
        choose(images, {"resolution": "native_only", "scale": "fit"})["selected_id"]
        == 1
    )
    assert "selected_id" not in choose(
        images, {"resolution": "native_only", "scale": "fill"}
    )


@pytest.mark.parametrize("removed", ["placement_id", "cell_w", "cell_h"])
def test_missing_placement_geometry_or_identity_skips_history(rotation, removed):
    _, _, ctx, choose = rotation
    context = {key: value for key, value in ctx.items() if key != removed}
    assert "selected_id" not in choose([image(1)], context=context)
    assert not (Path(ctx["data_dir"]) / "civitai-history.json").exists()


def test_cell_id_is_a_fallback_when_placement_id_is_unavailable(rotation):
    _, _, ctx, choose = rotation
    context = {key: value for key, value in ctx.items() if key != "placement_id"}
    context["cell_id"] = "legacy-cell"
    assert choose([image(1)], context=context)["selected_id"] == 1


@pytest.mark.parametrize(
    "value",
    [-1, 31, 1.5, True, "NaN", None, 10**5000],
    ids=["negative", "too-large", "fraction", "boolean", "nan", "null", "huge-int"],
)
def test_invalid_repeat_window_is_rejected_before_candidate_fetch(rotation, value):
    server, _, ctx, _ = rotation

    def unexpected(*args, **kwargs):
        pytest.fail("invalid settings must not fetch candidates")

    server._fetch_pool = unexpected
    result = server.fetch({"repeat_days": value}, {}, ctx=ctx)
    assert result["images"] == []
    assert "repeat" in result["error"].lower()


def test_engagement_minimums_apply_before_selection_and_keep_pending_metadata(rotation):
    _, _, _, choose = rotation
    stats = {
        "cryCount": 0,
        "laughCount": 0,
        "likeCount": 10,
        "heartCount": 0,
        "commentCount": 2,
    }
    result = choose(
        [image(1, priority=0), image(2, stats=stats)],
        {"min_reactions": 10, "min_comments": 2},
        metadata={"stale": True, "error": "Updating", "live_refresh_s": 2},
    )
    assert result["selected_id"] == 2
    assert [entry["id"] for entry in result["images"]] == [2]
    assert result["stale"] is True and result["live_refresh_s"] == 2
    assert result["error"] == "Updating"


@pytest.mark.parametrize(
    "contents",
    ["{broken", '{"placements": []}', "x" * (1024 * 1024 + 1)],
    ids=["invalid-json", "invalid-placements", "oversized"],
)
def test_corrupt_or_oversized_history_does_not_break_selection(rotation, contents):
    _, _, ctx, choose = rotation
    path = Path(ctx["data_dir"]) / "civitai-history.json"
    path.write_text(contents)
    assert choose([image(1)])["selected_id"] == 1
    assert path.stat().st_size <= 1024 * 1024
    assert isinstance(json.loads(path.read_text())["placements"], dict)


def test_history_is_bounded_by_placement_and_seen_image_count(rotation):
    _, _, ctx, choose = rotation
    path = Path(ctx["data_dir"]) / "civitai-history.json"
    path.write_text(
        json.dumps(
            {
                "placements": {
                    f"{placement:064x}": {
                        "updated": placement,
                        "seen": {str(ident): ident for ident in range(1, 700)},
                        "bucket": "old",
                        "selected": "1",
                    }
                    for placement in range(40)
                }
            }
        )
    )
    assert choose([image(1)])["selected_id"] == 1
    state = json.loads(path.read_text())
    assert len(state["placements"]) <= 32
    assert all(len(record["seen"]) <= 512 for record in state["placements"].values())
    assert path.stat().st_size <= 1024 * 1024
    assert not list(path.parent.glob("tmp*"))


def test_malformed_history_timestamps_and_fields_are_ignored(rotation):
    _, _, ctx, choose = rotation
    path = Path(ctx["data_dir"]) / "civitai-history.json"
    path.write_text(
        json.dumps(
            {
                "placements": {
                    "a" * 64: {"updated": 10**1000, "seen": []},
                    "b" * 64: {
                        "updated": 1,
                        "seen": {"1": 10**1000, "2": float("nan")},
                        "bucket": [],
                        "selected": {},
                    },
                }
            }
        )
    )
    assert choose([image(1)])["selected_id"] == 1
    assert "NaN" not in path.read_text()


def test_concurrent_placements_do_not_lose_each_others_history(rotation, monkeypatch):
    server, _, ctx, _ = rotation
    monkeypatch.setattr(
        server, "_fetch_pool", lambda *args, **kwargs: {"images": [image(1)]}
    )

    def select(index):
        context = {**ctx, "placement_id": f"page/cell-{index}@frame"}
        return server.fetch({}, {}, ctx=context)["selected_id"]

    with ThreadPoolExecutor(max_workers=4) as executor:
        assert list(executor.map(select, range(20))) == [1] * 20
    state = json.loads((Path(ctx["data_dir"]) / "civitai-history.json").read_text())
    assert len(state["placements"]) == 20


def test_delayed_request_cannot_rewind_a_newer_bucket_selection(rotation, monkeypatch):
    server, clock, _, choose = rotation
    assert choose([image(1)])["selected_id"] == 1
    images = [image(1), image(2), image(3)]
    original_lock = server._HIST_LOCK
    newer = []

    class InterleavedLock:
        first_entry = True

        def __enter__(self):
            if self.first_entry:
                self.first_entry = False
                # The older request reaches the lock, then a request in the
                # next bucket acquires it and writes history before it does.
                clock[0] += 3600
                newer.append(choose(images)["selected_id"])
            original_lock.acquire()

        def __exit__(self, *args):
            original_lock.release()

    monkeypatch.setattr(server, "_HIST_LOCK", InterleavedLock())
    delayed = choose(images)["selected_id"]
    assert delayed == newer[0]
    assert choose(images)["selected_id"] == newer[0]
