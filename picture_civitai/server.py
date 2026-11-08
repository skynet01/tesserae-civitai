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
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlsplit

API_URL = "https://civitai.com/api/v1/images"
TAG_URL = "https://civitai.com/api/trpc/tag.getAll"
MAX_IMAGES = 100
MAX_RENDER_WAIT_S = 4.5
REFRESH_DEADLINE_S = 4.0
# Image tag IDs verified against Civitai tag.getAll on 2026-11-07.
STYLE_TAG_IDS = {"drawing": 81, "illustration": 5145, "painting": 7660}
MAX_CALLS = 16
MAX_IMAGE_CALLS = 8
MAX_CACHE_BYTES = 2 * 1024 * 1024
_LOCK = threading.Lock()


class FeedError(ValueError):
    """A friendly configuration or response error."""


def _api_url(url: str) -> bool:
    if not isinstance(url, str) or any(ord(char) < 33 for char in url):
        return False
    try:
        parsed = urlsplit(url)
        return (
            parsed.scheme == "https"
            and parsed.netloc == "civitai.com"
            and parsed.path in ("/api/v1/images", "/api/trpc/tag.getAll")
            and not parsed.fragment
        )
    except ValueError:
        return False


class _ApiRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not _api_url(newurl):
            raise FeedError("Civitai redirected outside its supported API.")
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
        raise FeedError(
            "Only the fixed Civitai image and image-tag APIs are supported."
        )
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
            and parsed.netloc == "image.civitai.com"
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
    return {
        "id": entry["id"],
        "url": entry["url"],
        "width": entry["width"],
        "height": entry["height"],
        "username": username[:100] if isinstance(username, str) else "",
        "permalink": f"https://civitai.com/images/{entry['id']}",
        "tags": _tags(entry.get("tags")),
    }


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
    """Return a shared candidate pool; cell dimensions and rotation belong to the client."""
    placement = str(ctx.get("placement_id") or ctx.get("cell_id") or "")
    metadata = {"placement_id": placement}
    if _integer(ctx.get("cell_w")) and _integer(ctx.get("cell_h")):
        metadata.update(target_w=ctx["cell_w"], target_h=ctx["cell_h"])
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


def _fetch(
    options: dict[str, Any],
    settings: dict[str, Any],
    ctx: dict[str, Any],
    *,
    allow_network: bool = True,
) -> dict[str, Any]:
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
            or sort not in ("Most Reactions", "Most Collected", "Most Comments", "Newest")
            or period not in ("Day", "Week", "Month", "AllTime")
        ):
            raise FeedError("Choose a supported tag match, sort and period.")
        if (len(positives) if match == "any" else 1) * max(1, len(users)) > 8:
            raise FeedError(
                "Use at most 8 positive-tag and creator combinations, or choose all tags."
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
            [positives, negatives, users, match, sort, period, identity]
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
        return {"images": images}
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
                query = {"json": {"query": term, "entityType": ["Image"], "limit": 100}}
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
                    sample = request(f"{API_URL}?{urlencode({**base, 'limit': 200})}")
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

        groups = (
            [str(tag) for tag in resolved]
            if match == "any"
            else [",".join(map(str, resolved))]
        )
        queries = [
            {
                **base,
                **({"tags": group} if group else {}),
                **({"username": user} if user else {}),
            }
            for user, group in itertools.product(users or [""], groups or [""])
        ]
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
                    if users and clean["username"].casefold() not in users:
                        continue
                    if positives and not (
                        positive_ids <= ids if match == "all" else positive_ids & ids
                    ):
                        continue
                    if negatives and (
                        not clean["tags"]
                        or negative_ids & ids
                        or negative_names & names
                    ):
                        continue
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
        found = {}
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
        for row in itertools.zip_longest(*diverse_sources):
            for candidate in row:
                if candidate is not None:
                    found[candidate["id"]] = candidate
                    if len(found) >= MAX_IMAGES:
                        break
            if len(found) >= MAX_IMAGES:
                break
        images = list(found.values())
        state["pools"][key] = {"images": images, "fetched": now, "attempted": now}
        _save(path, state)
        return {"images": images}
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
