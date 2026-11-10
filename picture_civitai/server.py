"""Bounded public Civitai image pools, with local tag and creator verification."""

from __future__ import annotations

import contextlib
import contextvars
import hashlib
import itertools
import json
import math
import os
import re
import tempfile
import threading
import time
import urllib.error
import urllib.request
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlsplit

API_URL = "https://civitai.com/api/v1/images"
TAG_URL = "https://civitai.com/api/trpc/tag.getAll"
LEXICA_URL = "https://lexica.art/api/v1/search"
LEXICA_ID = r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}"
MAX_IMAGES = 100
MAX_RENDER_WAIT_S = 4.5
REFRESH_DEADLINE_S = 4.0
# Image tag IDs verified against Civitai tag.getAll on 2026-11-07.
STYLE_TAG_IDS = {"drawing": 81, "illustration": 5145, "painting": 7660}
MAX_CALLS = 16
MAX_IMAGE_CALLS = 8
MAX_CACHE_BYTES = 2 * 1024 * 1024
REACTION_FIELDS = ("cryCount", "laughCount", "likeCount", "heartCount")
STAT_FIELDS = (*REACTION_FIELDS, "commentCount")
_LOCK = threading.Lock()
_HIST_LOCK = threading.Lock()


class FeedError(ValueError):
    """A friendly configuration or response error."""


def _api_url(url: str) -> bool:
    if not isinstance(url, str) or any(ord(char) < 33 for char in url):
        return False
    try:
        parsed = urlsplit(url)
        return (
            parsed.scheme == "https"
            and (parsed.netloc, parsed.path)
            in (
                ("civitai.com", "/api/v1/images"),
                ("civitai.com", "/api/trpc/tag.getAll"),
                ("lexica.art", "/api/v1/search"),
            )
            and not parsed.fragment
        )
    except ValueError:
        return False


class _ApiRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if (
            not _api_url(newurl)
            or urlsplit(newurl).netloc != urlsplit(req.full_url).netloc
        ):
            raise FeedError("Gallery redirected outside its supported API.")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _api_opener() -> urllib.request.OpenerDirector:
    """Keep the literal hostname visible to Tesserae's domain capability hook."""
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        urllib.request.HTTPSHandler(),
        _ApiRedirect(),
    )


def fetch_json(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    timeout: float = 2,
    max_bytes: int = MAX_CACHE_BYTES,
) -> Any:
    """Fixed-domain HTTPS transport with a total body deadline and byte cap.

    DNS/connect are still subject to the operating system's timing; fetch()'s
    caller wait is bounded independently. read1 avoids a continuously streaming
    body extending the shared refresh indefinitely between deadline checks.
    """
    if not _api_url(url):
        raise FeedError("Only the fixed Civitai and Lexica APIs are supported.")
    if not 0 < timeout <= 2 or not 0 < max_bytes <= MAX_CACHE_BYTES:
        raise ValueError("Invalid Civitai network limits")
    deadline = time.monotonic() + timeout
    request = urllib.request.Request(url, headers=headers or {})
    opener = _api_opener()
    with opener.open(request, timeout=timeout) as response:
        body = bytearray()
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Civitai response deadline exceeded")
            if getattr(response, "isclosed", lambda: False)():
                break
            # urllib's HTTPS response wraps its socket in SocketIO.
            sock = getattr(
                getattr(getattr(response, "fp", None), "raw", None), "_sock", None
            )
            if sock is None:
                raise OSError("Civitai response socket is unavailable")
            sock.settimeout(min(2, remaining))
            chunk = response.read1(min(65536, max_bytes + 1 - len(body)))
            if time.monotonic() >= deadline:
                raise TimeoutError("Civitai response deadline exceeded")
            if not chunk:
                break
            body.extend(chunk)
            if len(body) > max_bytes:
                raise ValueError("Civitai response exceeds byte cap")
    return json.loads(body.decode("utf-8"))


def _terms(value: Any, limit: int, label: str) -> list[str]:
    if not isinstance(value, str) or len(value) > 4000:
        raise FeedError(f"Enter {label} as a comma-separated list.")
    terms = sorted(
        {term.strip().casefold() for term in re.split(r"[,\n]", value) if term.strip()}
    )
    if len(terms) > limit or any(len(term) > 100 for term in terms):
        raise FeedError(f"Use at most {limit} {label}, each at most 100 characters.")
    return terms


def _integer(value: Any) -> bool:
    return type(value) is int and 0 < value <= 2**53 - 1


def _minimum(value: Any, label: str) -> int:
    message = f"Choose a minimum {label} count from 0 to 1000000000, in whole numbers."
    if type(value) is int:
        if 0 <= value <= 1_000_000_000:
            return value
        raise FeedError(message)
    if type(value) not in (float, str) or len(str(value)) > 100:
        raise FeedError(message)
    if isinstance(value, str) and "_" in value:
        raise FeedError(message)
    try:
        number = Decimal(str(value).strip())
    except InvalidOperation:
        raise FeedError(message) from None
    if (
        not number.is_finite()
        or not 0 <= number <= 1_000_000_000
        or number != number.to_integral_value()
    ):
        raise FeedError(message)
    return int(number)


def _stats(raw: Any) -> dict[str, int]:
    if not isinstance(raw, dict):
        return {}
    return {
        field: raw[field]
        for field in STAT_FIELDS
        if type(raw.get(field)) is int and 0 <= raw[field] <= 2**53 - 1
    }


def _image_url(value: Any) -> bool:
    if (
        not isinstance(value, str)
        or len(value) > 2000
        or any(ord(c) < 33 for c in value)
    ):
        return False
    try:
        parsed = urlsplit(value)
        return (
            parsed.scheme == "https"
            and parsed.netloc in ("image.civitai.com", "image.lexica.art")
            and not parsed.fragment
            and bool(parsed.path)
            and not parsed.path.casefold().endswith(
                (".svg", ".html", ".gif", ".mp4", ".webm")
            )
        )
    except ValueError:
        return False


def _tags(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list) or len(raw) > 500:
        return []
    if any(
        not isinstance(tag, dict)
        or not _integer(tag.get("id"))
        or not isinstance(tag.get("name"), str)
        or not tag["name"].strip()
        for tag in raw
    ):
        return []
    return [
        {"id": tag["id"], "name": tag["name"][:100]}
        for tag in raw[:500]
        if isinstance(tag, dict)
        and _integer(tag.get("id"))
        and isinstance(tag.get("name"), str)
        and tag["name"].strip()
    ]


def _image(entry: Any) -> dict[str, Any] | None:
    if not isinstance(entry, dict) or not _integer(entry.get("id")):
        return None
    if (
        not _image_url(entry.get("url"))
        or urlsplit(entry["url"]).netloc != "image.civitai.com"
        or not _integer(entry.get("width"))
        or not _integer(entry.get("height"))
        or entry["width"] > 50000
        or entry["height"] > 50000
    ):
        return None
    if (
        entry.get("type") != "image"
        or entry.get("browsingLevel") != 1
        or entry.get("nsfwLevel") not in (1, "None")
        or entry.get("nsfw") is True
    ):
        return None
    username = entry.get("username")
    clean = {
        "id": entry["id"],
        "url": entry["url"],
        "width": entry["width"],
        "height": entry["height"],
        "username": username[:100] if isinstance(username, str) else "",
        "permalink": f"https://civitai.com/images/{entry['id']}",
        "tags": _tags(entry.get("tags")),
        "source_priority": 0
        if type(entry.get("source_priority")) is int and entry["source_priority"] == 0
        else 1,
    }
    stats = _stats(entry.get("stats"))
    if stats:
        clean["stats"] = stats
    return clean


def _load(path: Path) -> dict[str, Any]:
    try:
        if path.stat().st_size <= MAX_CACHE_BYTES:
            data = json.loads(path.read_text(encoding="utf-8"))
            if (
                isinstance(data, dict)
                and isinstance(data.get("pools"), dict)
                and isinstance(data.get("tags"), dict)
            ):
                pools = {
                    key: record
                    for key, record in list(data["pools"].items())[-8:]
                    if isinstance(record, dict)
                    and all(
                        isinstance(record.get(field, 0), (int, float))
                        and math.isfinite(record.get(field, 0))
                        for field in ("fetched", "attempted", "retry_at")
                    )
                }
                tags = {
                    name: record
                    for name, record in list(data["tags"].items())[-4096:]
                    if isinstance(record, dict)
                    and _integer(record.get("id"))
                    and isinstance(record.get("at"), (int, float))
                    and math.isfinite(record["at"])
                }
                retry = data.get("retry_at", 0)
                return {
                    "pools": pools,
                    "tags": tags,
                    "retry_at": retry
                    if isinstance(retry, (int, float)) and math.isfinite(retry)
                    else 0,
                }
    except (OSError, ValueError):
        pass
    return {"pools": {}, "tags": {}}


def _save(path: Path, state: dict[str, Any]) -> None:
    temporary = None
    try:
        state["pools"] = dict(
            sorted(
                state["pools"].items(), key=lambda pair: pair[1].get("attempted", 0)
            )[-8:]
        )
        state["tags"] = dict(list(state["tags"].items())[-4096:])
        encoded = json.dumps(state, separators=(",", ":"))
        if len(encoded.encode()) > MAX_CACHE_BYTES:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, delete=False
        ) as handle:
            temporary = handle.name
            handle.write(encoded)
        os.replace(temporary, path)
    except (OSError, TypeError, ValueError):
        pass
    finally:
        if temporary:
            with contextlib.suppress(OSError):
                os.unlink(temporary)


def _cached_images(record: dict[str, Any]) -> list[dict[str, Any]]:
    entries = record.get("images")
    if not isinstance(entries, list):
        return []
    return [
        clean
        for entry in entries[:MAX_IMAGES]
        if isinstance(entry, dict)
        and (
            clean := _image(
                {**entry, "type": "image", "nsfwLevel": "None", "browsingLevel": 1}
            )
        )
    ]


def fetch(
    options: dict[str, Any], settings: dict[str, Any], *, ctx: dict[str, Any]
) -> dict[str, Any]:
    """Apply placement minimums and durable rotation to the shared image pool."""
    pinned = options.get("pinned_image", "")
    if not isinstance(pinned, str) or (
        pinned.strip() and not _history_id(pinned.strip())
    ):
        return {
            "images": [],
            "error": "Choose a valid pinned artwork from Lineups, or clear the pin.",
        }
    pinned = pinned.strip()
    provider = options.get("provider", "civitai")
    if provider not in ("civitai", "lexica"):
        return {
            "images": [],
            "error": "Choose Civitai or Lexica as the gallery service.",
        }
    try:
        reactions = (
            _minimum(options.get("min_reactions", 0), "reactions")
            if provider == "civitai"
            else 0
        )
        comments = (
            _minimum(options.get("min_comments", 0), "comments")
            if provider == "civitai"
            else 0
        )
    except FeedError as err:
        return {"images": [], "error": str(err)}
    try:
        repeat_days = _minimum(options.get("repeat_days", 7), "repeat days")
        if repeat_days > 30:
            raise FeedError("Repeat period is too long.")
    except FeedError:
        return {
            "images": [],
            "error": "Choose a repeat avoidance period from 0 to 30 days, in whole numbers.",
        }
    result = _fetch_pool(options, settings, ctx=ctx)
    if reactions or comments:
        images = result["images"]
        filtered = []
        for candidate in images:
            stats = candidate.get("stats", {})
            if reactions and (
                not all(field in stats for field in REACTION_FIELDS)
                or sum(stats[field] for field in REACTION_FIELDS) < reactions
            ):
                continue
            if comments and stats.get("commentCount", -1) < comments:
                continue
            filtered.append(candidate)
        result = {
            **result,
            "images": filtered,
            **(
                {
                    "error": "No cached images meet the minimum reactions and comments. Lower the minimums or wait for the collection to refresh."
                }
                if images and not filtered
                else {}
            ),
        }
    if pinned:
        selected = next(
            (
                image
                for image in _eligible_for_context(result["images"], options, ctx)
                if str(image["id"]) == pinned
            ),
            None,
        )
        if selected is None:
            return {
                **result,
                "images": [],
                "error": "The pinned artwork is unavailable or does not match this frame and its filters. Choose another artwork or clear the pin.",
            }
        return {**result, "images": [selected], "selected_id": selected["id"]}
    if ctx.get("_lineup_cache_only"):
        return result
    return _select_rotation(result, options, ctx, repeat_days)


def _eligible_for_context(
    images: Any, options: dict[str, Any], ctx: dict[str, Any]
) -> list[dict[str, Any]]:
    width, height = ctx.get("cell_w"), ctx.get("cell_h")
    if not _integer(width) or not _integer(height):
        if (
            options.get("orientation") not in ("landscape", "portrait", "any")
            or options.get("resolution") != "allow_smaller"
        ):
            return []
        # Explicit orientation with unrestricted size does not depend on geometry.
        width = height = 1
    return _rotation_candidates(images, options, width, height)


def _thumbnail(image: dict[str, Any], width: int = 480) -> str:
    if image.get("provider") == "lexica":
        return image.get("thumbnail", image["url"])
    return re.sub(
        r"/(?:original=true|width=\d+[^/]*)/",
        f"/width={min(width, image['width'])}/",
        image["url"],
        count=1,
    )


def lineup_items(
    options: dict[str, Any] | None = None, *, ctx: dict[str, Any] | None = None
) -> list[dict[str, str]]:
    """List this frame's eligible cached artworks without fetching or rotating."""
    from flask import current_app, has_app_context

    if not has_app_context():
        return []
    registry = current_app.config.get("PLUGIN_REGISTRY")
    plugin = registry.get("picture_civitai") if registry is not None else None
    if plugin is None:
        return []
    effective = {
        field["name"]: field.get("default")
        for field in plugin.manifest.get("cell_options", [])
    }
    effective.update(options or {})
    effective["pinned_image"] = ""
    context = {
        "data_dir": str(plugin.data_dir),
        **(ctx or {}),
        "preview": True,
        "settled": False,
        "_lineup_cache_only": True,
    }
    store = current_app.config.get("SETTINGS_STORE")
    settings = (
        store.get_for_runtime(
            "plugins", "picture_civitai", plugin.manifest.get("settings", [])
        )
        if store is not None
        else {}
    )
    result = fetch(effective, settings, ctx=context)
    images = _eligible_for_context(result.get("images"), effective, context)
    return [
        {
            "key": str(image["id"]),
            "label": f"{image['username']} / {image['id']}"
            if image.get("username")
            else f"Lexica / {image.get('prompt', '')[:120]}"
            if image.get("provider") == "lexica"
            else f"Artwork {image['id']}",
            "thumb": _thumbnail(image, 320),
        }
        for image in images[:MAX_IMAGES]
    ]


def query_preview(
    options: dict[str, Any], settings: dict[str, Any], *, ctx: dict[str, Any]
) -> dict[str, Any]:
    """Show the same eligible pool as a display, without advancing history."""
    result = fetch(
        {**options, "pinned_image": ""},
        settings,
        ctx={**ctx, "preview": True, "settled": False},
    )
    if not result.get("images"):
        return {**result, "total": 0}
    width, height = ctx.get("cell_w"), ctx.get("cell_h")
    if not _integer(width) or not _integer(height):
        return {
            "images": [],
            "total": 0,
            "error": "Choose a target cell size to preview these filters.",
        }
    pool = _rotation_candidates(result.get("images"), options, width, height)
    positives = _terms(options.get("positive_tags", ""), 6, "positive tags")
    users = (
        _terms(options.get("users", ""), 4, "creators")
        if options.get("provider", "civitai") == "civitai"
        else []
    )
    images = []
    for image in pool[:20]:
        matches = []
        if image.get("provider") == "lexica":
            matches.append(f"Search: {options.get('lexica_query', '').strip()}")
            matches.extend(
                f"Prompt: {term}"
                for term in positives
                if _prompt_match(image["prompt"], term)
            )
        else:
            if image["username"].casefold() in users:
                matches.append(f"Creator: {image['username']}")
            names = {tag["name"].casefold() for tag in image["tags"]}
            ids = {str(tag["id"]) for tag in image["tags"]}
            matches.extend(
                f"Tag: {term}" for term in positives if term in names or term in ids
            )
        thumbnail = _thumbnail(image)
        images.append(
            {**image, "thumbnail": thumbnail, "matches": matches or ["Public feed"]}
        )
    return {
        "images": images,
        "total": len(pool),
        **{
            key: result[key]
            for key in ("error", "warning", "stale", "live_refresh_s")
            if key in result
        },
    }


def _rotation_candidates(
    images: Any, options: dict[str, Any], width: int, height: int
) -> list[dict[str, Any]]:
    """Mirror client eligibility before history or creator preferences apply."""

    def shape(w: int, h: int) -> str:
        return "landscape" if w > h else "portrait" if w < h else "square"

    orientation = options.get("orientation")
    if orientation not in ("landscape", "portrait", "any"):
        orientation = shape(width, height)
    pool = {}
    for image in images if isinstance(images, list) else []:
        if (
            not isinstance(image, dict)
            or not (
                _integer(image.get("id"))
                or (image.get("provider") == "lexica" and _history_id(image.get("id")))
            )
            or not _integer(image.get("width"))
            or not _integer(image.get("height"))
            or not _image_url(image.get("url"))
        ):
            continue
        if (
            orientation != "any"
            and shape(image["width"], image["height"]) != orientation
        ):
            continue
        pool.setdefault(str(image["id"]), image)
    candidates = list(pool.values())
    if options.get("resolution") != "allow_smaller":
        scale = min if options.get("scale") == "fit" else max
        native = [
            image
            for image in candidates
            if scale(width / image["width"], height / image["height"]) <= 1
        ]
        if native or options.get("resolution") == "native_only":
            candidates = native
    return candidates


def _history_time(value: Any) -> bool:
    return (
        type(value) in (int, float) and 0 <= value <= 2**53 - 1 and math.isfinite(value)
    )


def _history_id(value: Any) -> bool:
    return isinstance(value, str) and (
        (
            re.fullmatch(r"[1-9][0-9]{0,15}", value) is not None
            and int(value) <= 2**53 - 1
        )
        or re.fullmatch(f"lexica:{LEXICA_ID}", value) is not None
    )


def _read_history(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as handle:
            raw = handle.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            return {}
        data = json.loads(raw)
        records = data.get("placements") if isinstance(data, dict) else None
        if not isinstance(records, dict):
            return {}
        cleaned = {}
        for key, record in records.items():
            if (
                not isinstance(key, str)
                or re.fullmatch(r"[0-9a-f]{64}", key) is None
                or not isinstance(record, dict)
                or not _history_time(record.get("updated"))
            ):
                continue
            seen = record.get("seen")
            seen = (
                {
                    ident: timestamp
                    for ident, timestamp in seen.items()
                    if _history_id(ident) and _history_time(timestamp)
                }
                if isinstance(seen, dict)
                else {}
            )
            cleaned[key] = {
                "updated": record["updated"],
                "seen": dict(sorted(seen.items(), key=lambda item: item[1])[-512:]),
                "bucket": record.get("bucket")
                if isinstance(record.get("bucket"), str)
                and len(record["bucket"]) <= 100
                else "",
                "selected": record.get("selected")
                if _history_id(record.get("selected"))
                else "",
            }
        return dict(sorted(cleaned.items(), key=lambda item: item[1]["updated"])[-32:])
    except (OSError, ValueError):
        return {}


def _write_history(path: Path, records: dict[str, Any]) -> None:
    temporary = None
    try:
        records = dict(
            sorted(records.items(), key=lambda item: item[1]["updated"])[-32:]
        )
        for record in records.values():
            record["seen"] = dict(
                sorted(record["seen"].items(), key=lambda item: item[1])[-512:]
            )
        encoded = json.dumps(
            {"placements": records}, separators=(",", ":"), allow_nan=False
        )
        if len(encoded.encode()) > 1024 * 1024:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, delete=False
        ) as handle:
            temporary = handle.name
            handle.write(encoded)
        os.replace(temporary, path)
    except (OSError, TypeError, ValueError):
        pass
    finally:
        if temporary:
            with contextlib.suppress(OSError):
                os.unlink(temporary)


def _select_rotation(
    result: dict[str, Any],
    options: dict[str, Any],
    ctx: dict[str, Any],
    repeat_days: int,
) -> dict[str, Any]:
    placement = ctx.get("placement_id") or ctx.get("cell_id")
    width, height = ctx.get("cell_w"), ctx.get("cell_h")
    if (
        not placement
        or not _integer(width)
        or not _integer(height)
        or not ctx.get("data_dir")
    ):
        return result
    pool = _rotation_candidates(result.get("images"), options, width, height)
    if not pool:
        return result
    key = hashlib.sha256(str(placement).encode()).hexdigest()
    path = Path(ctx["data_dir"]) / "civitai-history.json"
    with _HIST_LOCK:
        # Capture time after acquiring the lock: a waiting request must not
        # overwrite a newer bucket with the timestamp from before it waited.
        now = time.time()
        try:
            minutes = float(options.get("rotation_minutes", 60))
            interval = minutes * 60
            if not math.isfinite(interval) or interval <= 0:
                interval = 3600
        except (TypeError, ValueError, OverflowError):
            interval = 3600
        bucket = f"{interval}:{math.floor(now / interval)}"
        records = _read_history(path)
        record = records.get(key, {})
        if record.get("bucket") == bucket:
            selected = next(
                (image for image in pool if str(image["id"]) == record.get("selected")),
                None,
            )
            if selected:
                return {**result, "selected_id": selected["id"]}
        seen = record.get("seen", {})
        cutoff = now - repeat_days * 86400
        rested = [
            image for image in pool if seen.get(str(image["id"]), -math.inf) <= cutoff
        ]
        candidates = rested if repeat_days and rested else pool

        def order(image: dict[str, Any]) -> tuple[Any, ...]:
            ident = str(image["id"])
            priority = 0 if image.get("source_priority") == 0 else 1
            tie = hashlib.sha256(f"{key}:{bucket}:{ident}".encode()).hexdigest()
            if repeat_days and not rested:
                return seen.get(ident, -math.inf), priority, tie
            return priority, tie

        selected = min(candidates, key=order)
        preview = bool(ctx.get("preview"))
        if not preview and ctx.get("settled", not preview):
            # This records the scheduled primary, not a delivery acknowledgement.
            # A client-side image-load fallback does not write another history entry.
            records[key] = {
                "updated": now,
                "seen": {**seen, str(selected["id"]): now},
                "bucket": bucket,
                "selected": str(selected["id"]),
            }
            _write_history(path, records)
    return {**result, "selected_id": selected["id"]}


def _fetch_pool(
    options: dict[str, Any], settings: dict[str, Any], *, ctx: dict[str, Any]
) -> dict[str, Any]:
    """Return a shared candidate pool; cell dimensions and rotation belong to the client."""
    placement = str(ctx.get("placement_id") or ctx.get("cell_id") or "")
    metadata = {"placement_id": placement}
    if _integer(ctx.get("cell_w")) and _integer(ctx.get("cell_h")):
        metadata.update(target_w=ctx["cell_w"], target_h=ctx["cell_h"])
    if ctx.get("_lineup_cache_only"):
        return {**_fetch(options, settings, ctx, allow_network=False), **metadata}
    if not _LOCK.acquire(blocking=False):
        return {
            **_fetch(options, settings, ctx, allow_network=False),
            **metadata,
        }
    result: list[dict[str, Any]] = []
    failures: list[BaseException] = []
    copied_context = contextvars.copy_context()
    options, settings, ctx = dict(options), dict(settings), dict(ctx)

    def refresh() -> None:
        try:
            result.append(_fetch(options, settings, ctx))
        except BaseException as err:
            failures.append(err)
        finally:
            _LOCK.release()

    worker = threading.Thread(target=lambda: copied_context.run(refresh), daemon=True)
    try:
        worker.start()
    except RuntimeError:
        _LOCK.release()
        return {
            "images": [],
            "error": "Civitai collection is updating. Try again shortly.",
            **metadata,
        }
    worker.join(MAX_RENDER_WAIT_S)
    if failures:
        raise failures[0]
    return {
        **(
            result[0] if result else _fetch(options, settings, ctx, allow_network=False)
        ),
        **metadata,
    }


def _prompt_match(prompt: str, term: str) -> bool:
    return (
        re.search(r"(?<!\w)" + re.escape(term) + r"(?!\w)", prompt.casefold())
        is not None
    )


def _lexica_image(entry: Any) -> dict[str, Any] | None:
    if not isinstance(entry, dict):
        return None
    ident = entry.get("id")
    if not isinstance(ident, str) or re.fullmatch(LEXICA_ID, ident) is None:
        return None
    if (
        entry.get("nsfw") is not False
        or entry.get("grid") is not False
        or not _integer(entry.get("width"))
        or not _integer(entry.get("height"))
        or max(entry["width"], entry["height"]) > 50000
        or not isinstance(entry.get("prompt"), str)
        or len(entry["prompt"]) > 8000
        or not _image_url(entry.get("src"))
        or urlsplit(entry["src"]).netloc != "image.lexica.art"
    ):
        return None
    thumbnail = entry.get("srcSmall")
    return {
        "id": f"lexica:{ident}",
        "provider": "lexica",
        "url": entry["src"],
        "thumbnail": thumbnail
        if _image_url(thumbnail) and urlsplit(thumbnail).netloc == "image.lexica.art"
        else entry["src"],
        "width": entry["width"],
        "height": entry["height"],
        "prompt": entry["prompt"],
        "permalink": f"https://lexica.art/?q={ident}",
        "username": "",
        "tags": [],
        "source_priority": 1,
    }


def _fetch_lexica(
    options: dict[str, Any], ctx: dict[str, Any], *, allow_network: bool
) -> dict[str, Any]:
    try:
        query = options.get("lexica_query", "")
        if not isinstance(query, str) or not 1 <= len(query.strip()) <= 300:
            raise FeedError("Enter a Lexica search of 1 to 300 characters.")
        query = query.strip()
        positives = _terms(options.get("positive_tags", ""), 6, "positive prompt terms")
        negatives = _terms(
            options.get("negative_tags", ""), 50, "negative prompt terms"
        )
        match = options.get("tag_match", "any")
        if match not in ("any", "all"):
            raise FeedError("Choose Any or All for prompt matching.")
        refresh = float(options.get("refresh_hours", 24))
        if not 1 <= refresh <= 168:
            raise FeedError("Choose a collection refresh between 1 and 168 hours.")
    except (FeedError, TypeError, ValueError) as err:
        return {
            "images": [],
            "error": str(err)
            if isinstance(err, FeedError)
            else "Check the collection refresh setting.",
        }
    path = Path(ctx["data_dir"]) / "lexica-cache.json"
    state = _load(path)
    key = hashlib.sha256(query.encode()).hexdigest()
    record = state["pools"].get(key, {})
    now = time.time()
    metadata = {}
    fresh = 0 <= now - record.get("fetched", 0) < refresh * 3600
    if (
        not fresh
        and now >= max(record.get("retry_at", 0), state.get("retry_at", 0))
        and allow_network
    ):
        try:
            response = fetch_json(
                f"{LEXICA_URL}?{urlencode({'q': query})}",
                headers={
                    "Accept": "application/json",
                    "User-Agent": "tesserae (+picture_civitai)",
                },
            )
            if not isinstance(response, dict) or not isinstance(
                response.get("images"), list
            ):
                raise FeedError("Lexica returned an unreadable image collection.")
            raw = [entry for entry in response["images"][:50] if _lexica_image(entry)]
            record = {"images": raw, "fetched": now, "attempted": now}
        except (OSError, ValueError, TimeoutError) as err:
            delay = 60
            if isinstance(err, urllib.error.HTTPError):
                if err.code == 429:
                    delay = 120
                    with contextlib.suppress(ValueError, TypeError):
                        delay = max(
                            60, min(3600, int(err.headers.get("Retry-After", "120")))
                        )
                    state["retry_at"] = now + delay
                err.close()
            record = {
                **record,
                "error": "Could not load Lexica images. Try again shortly.",
                "retry_at": now + delay,
                "attempted": now,
            }
        state["pools"][key] = record
        _save(path, state)
    elif not fresh and now < state.get("retry_at", 0):
        metadata = {"error": "Lexica is rate limited. Try again shortly."}
    elif not fresh and not allow_network:
        metadata = {
            "error": "Lexica collection is updating. Try again shortly.",
            "live_refresh_s": 2,
        }
    if "error" in record:
        metadata["error"] = record["error"]
    raw = record.get("images", [])
    images = []
    for entry in raw[:50] if isinstance(raw, list) else []:
        image = _lexica_image(entry)
        if image is None:
            continue
        prompt = image["prompt"]
        if any(_prompt_match(prompt, term) for term in negatives):
            continue
        if positives and not (all if match == "all" else any)(
            _prompt_match(prompt, term) for term in positives
        ):
            continue
        images.append(image)
    if images and metadata:
        metadata["stale"] = True
    return {"images": images, **metadata}


def _fetch(
    options: dict[str, Any],
    settings: dict[str, Any],
    ctx: dict[str, Any],
    *,
    allow_network: bool = True,
) -> dict[str, Any]:
    if options.get("provider") == "lexica":
        return _fetch_lexica(options, ctx, allow_network=allow_network)
    now = time.time()
    try:
        positives = _terms(options.get("positive_tags", ""), 6, "positive tags")
        negatives = _terms(options.get("negative_tags", ""), 50, "negative tags")
        users = _terms(options.get("users", ""), 4, "creators")
        if any(not re.fullmatch(r"[\w.-]+", user) for user in users):
            raise FeedError("Enter creator usernames, without profile URLs or @ signs.")
        match = options.get("tag_match", "any")
        sort = options.get("sort", "Most Reactions")
        period = options.get("period", "Week")
        if (
            match not in ("any", "all")
            or sort
            not in ("Most Reactions", "Most Collected", "Most Comments", "Newest")
            or period not in ("Day", "Week", "Month", "AllTime")
        ):
            raise FeedError("Choose a supported tag match, sort and period.")
        if len(users) + (len(positives) if match == "any" else bool(positives)) > 8:
            raise FeedError(
                "Use at most 8 creator and positive-tag sources in total, or choose all tags."
            )
        refresh = float(options.get("refresh_hours", 24))
        if not 1 <= refresh <= 168:
            raise FeedError("Choose a collection refresh between 1 and 168 hours.")
        api_key = settings.get("api_key_secret", settings.get("api_key", ""))
        if (
            not isinstance(api_key, str)
            or len(api_key) > 1000
            or any(ord(c) < 32 for c in api_key)
        ):
            raise FeedError("The Civitai API key is invalid.")
        api_key = api_key.strip()
    except (FeedError, TypeError, ValueError) as err:
        return {
            "images": [],
            "error": str(err)
            if isinstance(err, FeedError)
            else "Check the collection refresh setting.",
        }

    identity = (
        hashlib.sha256(api_key.encode()).hexdigest()[:16] if api_key else "public"
    )
    key = hashlib.sha256(
        json.dumps(
            [
                "stats-creators-v1",
                positives,
                negatives,
                users,
                match,
                sort,
                period,
                identity,
            ]
        ).encode()
    ).hexdigest()
    path = Path(ctx["data_dir"]) / "civitai-cache.json"
    state = _load(path)
    record = state["pools"].get(key, {})
    if not isinstance(record, dict):
        record = {}
    images = _cached_images(record)
    fetched = record.get("fetched", 0)
    if isinstance(fetched, (int, float)) and 0 <= now - fetched < refresh * 3600:
        return {
            "images": images,
            **(
                {"warning": record["warning"]}
                if isinstance(record.get("warning"), str)
                else {}
            ),
        }
    if now < max(state.get("retry_at", 0), record.get("retry_at", 0)):
        result = {
            "images": images,
            "error": record.get("error", "Civitai is rate limited. Try again shortly."),
        }
        if images:
            result["stale"] = True
        return result

    if not allow_network:
        return {
            "images": images,
            "error": "Civitai collection is updating. Try again shortly.",
            "live_refresh_s": 2,
            **({"stale": True} if images else {}),
        }

    deadline = time.monotonic() + REFRESH_DEADLINE_S
    calls = 0
    headers = {
        "User-Agent": "tesserae (+picture_civitai)",
        "Accept": "application/json",
    }
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    def request(url: str) -> Any:
        nonlocal calls
        remaining = deadline - time.monotonic()
        if calls >= MAX_CALLS or remaining <= 0:
            raise FeedError(
                "Civitai collection lookup reached its request limit. Try fewer filters."
            )
        calls += 1
        return fetch_json(
            url, headers=headers, timeout=min(2, remaining), max_bytes=MAX_CACHE_BYTES
        )

    base = {
        "limit": 200,
        "browsingLevel": 1,
        "type": "image",
        "withTags": "true",
        "sort": sort,
        "period": period,
    }

    def remember_tags(entries: list[Any]) -> None:
        for entry in entries[:200]:
            if isinstance(entry, dict):
                for tag in _tags(entry.get("tags")):
                    state["tags"][f"{identity}:{tag['name'].casefold()}"] = {
                        "id": tag["id"],
                        "at": now,
                    }

    try:
        resolved = []
        sampled = False
        tag_warning = ""
        try:
            for term in positives:
                if term in STYLE_TAG_IDS:
                    resolved.append(STYLE_TAG_IDS[term])
                    continue
                if term.isascii() and term.isdecimal() and _integer(int(term)):
                    resolved.append(int(term))
                    continue
                saved = state["tags"].get(f"{identity}:{term}", {})
                if not isinstance(saved, dict) or now - saved.get("at", 0) > 30 * 86400:
                    saved = {}
                if not saved and api_key:
                    query = {
                        "json": {"query": term, "entityType": ["Image"], "limit": 100}
                    }
                    response = request(
                        f"{TAG_URL}?{urlencode({'input': json.dumps(query)})}"
                    )
                    try:
                        payload = response["result"]["data"]["json"]
                        tags = (
                            payload.get("items", [])
                            if isinstance(payload, dict)
                            else payload
                        )
                    except (TypeError, KeyError):
                        raise FeedError(
                            "Civitai tag lookup is unavailable. Try a numeric image tag ID."
                        ) from None
                    for tag in _tags(tags):
                        if tag["name"].casefold() == term:
                            saved = {"id": tag["id"], "at": now}
                            state["tags"][f"{identity}:{term}"] = saved
                            break
                elif not saved:
                    if not sampled:
                        sample = request(
                            f"{API_URL}?{urlencode({**base, 'limit': 200})}"
                        )
                        if not isinstance(sample, dict) or not isinstance(
                            sample.get("items"), list
                        ):
                            raise FeedError(
                                "Civitai returned an unreadable image collection."
                            )
                        remember_tags(sample["items"])
                        sampled = True
                    saved = state["tags"].get(f"{identity}:{term}", {})
                if not _integer(saved.get("id")):
                    raise FeedError(
                        f"Could not find image tag '{term}'. Add a Civitai API key in widget settings for tag lookup, or enter its numeric tag ID."
                    )
                resolved.append(saved["id"])
        except FeedError as err:
            if not users:
                raise
            resolved = []
            tag_warning = str(err)
        groups = (
            [str(tag) for tag in resolved]
            if match == "any"
            else [",".join(map(str, resolved))]
            if resolved
            else []
        )
        queries = (
            [{**base, "username": user} for user in users]
            + [{**base, "tags": group} for group in groups]
        ) or [base]
        positive_ids = set(resolved)
        negative_ids = {
            int(term) for term in negatives if term.isascii() and term.isdecimal()
        }
        negative_names = set(negatives)
        sources: list[dict[int, dict[str, Any]]] = [{} for _ in queries]
        source_indexes = list(range(len(queries)))
        cursors: list[set[str]] = [set() for _ in queries]
        image_calls = 0
        while queries and image_calls < MAX_IMAGE_CALLS:
            remaining_queries = []
            remaining_cursors = []
            remaining_indexes = []
            for query, seen, source_index in zip(
                queries, cursors, source_indexes, strict=True
            ):
                if image_calls >= MAX_IMAGE_CALLS:
                    break
                response = request(f"{API_URL}?{urlencode(query)}")
                image_calls += 1
                if not isinstance(response, dict) or not isinstance(
                    response.get("items"), list
                ):
                    raise FeedError("Civitai returned an unreadable image collection.")
                remember_tags(response["items"])
                for entry in response["items"][:200]:
                    clean = _image(entry)
                    if clean is None:
                        continue
                    ids = {tag["id"] for tag in clean["tags"]}
                    names = {tag["name"].casefold() for tag in clean["tags"]}
                    creator = query.get("username")
                    if creator and clean["username"].casefold() != creator:
                        continue
                    if (
                        not creator
                        and positives
                        and not (
                            positive_ids <= ids
                            if match == "all"
                            else positive_ids & ids
                        )
                    ):
                        continue
                    if negatives and (
                        not clean["tags"]
                        or negative_ids & ids
                        or negative_names & names
                    ):
                        continue
                    clean["source_priority"] = 0 if creator else 1
                    sources[source_index][clean["id"]] = clean
                metadata = response.get("metadata", {})
                cursor = (
                    metadata.get("nextCursor") if isinstance(metadata, dict) else None
                )
                if (
                    isinstance(cursor, str)
                    and 0 < len(cursor) <= 1000
                    and cursor not in seen
                    and not sources[source_index]
                    and "cursor" not in query
                ):
                    seen.add(cursor)
                    remaining_queries.append({**query, "cursor": cursor})
                    remaining_cursors.append(seen)
                    remaining_indexes.append(source_index)
            if len(set().union(*(source.keys() for source in sources))) >= MAX_IMAGES:
                break
            queries, cursors, source_indexes = (
                remaining_queries,
                remaining_cursors,
                remaining_indexes,
            )
        diverse_sources = []
        for source in sources:
            orientations = [[], [], []]
            for candidate in source.values():
                orientation = (
                    0
                    if candidate["width"] > candidate["height"]
                    else 1
                    if candidate["width"] < candidate["height"]
                    else 2
                )
                orientations[orientation].append(candidate)
            diverse_sources.append(
                [
                    candidate
                    for row in itertools.zip_longest(*orientations)
                    for candidate in row
                    if candidate is not None
                ]
            )
        priorities: list[list[dict[str, Any]]] = [[], []]
        seen = set()
        for priority, group in enumerate(
            (diverse_sources[: len(users)], diverse_sources[len(users) :])
        ):
            for row in itertools.zip_longest(*group):
                for candidate in row:
                    if candidate is not None and candidate["id"] not in seen:
                        seen.add(candidate["id"])
                        priorities[priority].append(candidate)
        creators, discoveries = priorities
        creator_count = min(
            len(creators), MAX_IMAGES * 3 // 4 if discoveries else MAX_IMAGES
        )
        discovery_count = min(len(discoveries), MAX_IMAGES - creator_count)
        creator_count = min(len(creators), MAX_IMAGES - discovery_count)
        images = creators[:creator_count] + discoveries[:discovery_count]
        warning = {"warning": tag_warning} if tag_warning else {}
        state["pools"][key] = {
            "images": images,
            "fetched": now,
            "attempted": now,
            **warning,
        }
        _save(path, state)
        return {"images": images, **warning}
    except urllib.error.HTTPError as err:
        if err.code == 429:
            delay = 120
            with contextlib.suppress(ValueError, TypeError):
                delay = max(60, min(3600, int(err.headers.get("Retry-After", "120"))))
            state["retry_at"] = now + delay
            message = "Civitai is rate limited. Try again shortly."
        elif err.code in (401, 403):
            message = "Civitai refused access. Check your API key, or use numeric image tag IDs."
        else:
            message = "Civitai is unavailable. Try again shortly."
        err.close()
    except FeedError as err:
        message = str(err)
    except (OSError, ValueError, TimeoutError):
        message = "Could not load Civitai images. Try again shortly."
    state["pools"][key] = {
        **record,
        "images": images,
        "attempted": now,
        "retry_at": now + 60,
        "error": message,
    }
    _save(path, state)
    return {"images": images, "error": message, **({"stale": True} if images else {})}
