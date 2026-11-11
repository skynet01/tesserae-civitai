# AI gallery picture widget for Tesserae

Rotating artwork from [Civitai](https://civitai.com) for [Tesserae](https://github.com/dmellok/tesserae). Images fill the widget area, with an optional quiet creator credit. Configuration uses Tesserae's normal widget settings and cell options. The widget ID remains `picture_civitai`, and existing dashboards keep using Civitai.

## Query preview

**Preview query** opens a popup with up to 20 images from the eligible cached collection, using the current unsaved filters and target cell dimensions. Badges over each image show the matching creators, tags, and collections. Click an image to open its original page. Preview does not advance rotation or change repeat history. Counts describe the bounded cached collection, rather than every result available from the service.

The preview button requires Tesserae's query-preview editor hook (included in the companion core change). Older hosts can still render the widget, but do not show the button.

Lexica is temporarily disabled. Saved Lexica or mixed-gallery settings use Civitai. Clear a Lexica artwork pin to resume rotation.

## Install

Install through **Settings > Widgets > Browse community widgets** when this repository is listed in the catalog. For a manual installation, copy the `picture_civitai` folder into your Tesserae checkout's `plugins/` folder and restart Tesserae. Keep the folder name: it is the widget ID.

Add **Picture, AI galleries** as the only widget on a full-display page for a full-screen art frame. It starts with edge-to-edge **Fill** scaling and automatic orientation, showing **illustration or painting**, excluding **photography and photorealistic** tags, sorted by most reactions for the month. Leaving tags and creators blank uses the general public SFW image feed.

## Filters

**Civitai artwork sources** has independent checkboxes for **Tags**, **Creators**, and **My and followed collections**.
Use any combination, or select only collections. Existing dashboards keep Tags and Creators selected. **Choose collections**
lists image collections you own (including private ones) and follow, with a checkbox for each. Your saved key needs collection
read access. Up to eight collections can be selected; this is separate from the existing eight tag/creator source limit.
For a collection-only rotation, choose **Civitai** as the gallery service and uncheck Tags and Creators.

Collection picks bypass include tags, just as creator picks do. Exclusions, minimum engagement, orientation and resolution
still apply. Collections and creators take priority over tag discoveries, and duplicate images appear only once. Query previews
identify the collection an image came from. Lineups previews and pins use the same cached eligible pool without advancing history.

The picker refreshes at most once every 15 minutes per API key. Each selected collection has its own credential-isolated image
cache, refreshed using **Collection refresh**. Changing exclusions or minimum engagement reuses the cached images. Collection
requests share a four-second refresh budget and back off after failures; stale usable images and other selected sources remain
available. Collection access uses Civitai's read-only site API endpoints, which may change independently of the public REST API.

- **Include tags:** comma- or newline-separated names or numeric tag IDs. Tag discoveries match any included tag by default (OR); **More options > Include tag matching > All tags** requires every tag for those discoveries.

- **Creators:** comma- or newline-separated Civitai usernames. Creator artwork takes priority and does not need an included tag. Tag discoveries provide additional candidates after eligible creator picks have been used recently. Exclusions and engagement minimums still apply to both sources.
- **Exclude tags:** every exclusion applies. An image with any excluded tag is rejected, including when that tag is also included. Names and numeric IDs are supported.

Civitai names must match an image tag exactly, including words and plural forms; for example, `outdoors` is different
from `outdoor`. Spaces within a name are supported. An API key enables lookup but does not turn arbitrary phrases into tags.

Creator filters, negative tag names and the default illustration/painting preset work without an API key. The preset's verified image tag IDs are built in, so it needs no tag lookup requests. Without a key, other positive tag names are resolved from a sample of the popular public feed; names absent from that sample produce a message asking for an API key or numeric tag ID. This sample is not Civitai's complete tag index.

**More options > Minimum reactions / Minimum comments** can require a minimum amount of engagement. For example, set Minimum reactions to `100` to keep images with at least 100 combined likes, hearts, laughs and cries. Dislikes are excluded. Both minimums default to `0` (disabled); enabled minimums must both be met. Images with missing or invalid required counts are skipped. These are the counts returned by Civitai, rather than a star rating or a guarantee of image quality. The image API supports Most collected ordering but does not return a collection count for a collection minimum.

Minimums filter the existing cached collection locally. Raising, lowering or disabling a minimum makes no additional API requests. Counts update when the collection refreshes; a narrow threshold can exhaust the sampled pool, in which case the widget asks you to lower it or wait for the next refresh. Upgrading from a release that did not cache counts fetches the collection once to populate them.

For full positive tag-name lookup, create a free personal API key at [Civitai account settings](https://civitai.com/user/account) and save it in **Settings > Plugins > Picture, AI galleries > Civitai API key**. Tesserae stores it as a secret and does not send it to the widget client. The widget uses Civitai's public image API and an internal tRPC image-tag lookup endpoint. The internal endpoint requires authentication and may change independently of the public API.

Only public images marked SFW by Civitai are requested. Filtering depends on Civitai's metadata; it does not inspect image contents. Candidate collection is bounded, so narrow combinations can have no matches even when matching images exist elsewhere on Civitai.

Creator sources are fetched first, followed by independent tag sources, with at most eight sources total. When both have matches, the cache reserves up to 75 creator images and 25 tag discoveries, filling unused space from either side. A creator match wins if both sources contain the same image. If a tag name cannot be resolved, valid creator picks remain available with a warning.

The manifest limits network access to `civitai.com` and `image.civitai.com`. The widget accepts only its fixed Civitai HTTPS API paths, rejects redirects outside them, and validates Civitai image URLs against its CDN. Downloads use normal TLS verification and enforce response deadlines and size limits.

## Lineups artwork browsing

The Lineups page and lineup editor can browse individual artworks with left/right arrows, show thumbnail previews,
push the selected artwork, and save it with **Show chosen**. **Pinned artwork** under More options holds the selected
key; clear it to resume normal rotation. Pins still obey source filters, minimum engagement, orientation and resolution.
If a pinned image leaves the eligible collection, the widget shows an explanation instead of silently changing it.
A pinned image-load failure also never substitutes a different artwork. Browsing, query previews and pinned renders
do not advance repeat history.

The item list reads only the cached collection, never the gallery API, and contains at most 100 artworks. A normal widget render or query preview populates the cache first. The list uses the same physical cell
dimensions as rendering, including Auto orientation and native-size rules. On an older host that cannot supply those
dimensions, Auto and native-size modes return no items; explicit orientation with Allow smaller images can still list
cached matches. Per-item previews and geometry-aware listing require the companion Lineups host update.

The callback is `lineup_items(options, *, ctx=None)`, with stable string image IDs as `key`, a creator/image label,
and `thumb` pointing to a Civitai 320 px CDN variant where available. Listing merges manifest defaults with the saved cell options, uses only the current API-key
identity's Civitai pool, and ignores the saved pin so the editor can browse or choose a replacement.

## Orientation and image size

**Auto** uses the actual cell dimensions, even when the cell and whole display have different orientations. A landscape cell accepts landscape images, a portrait cell accepts portrait images, and a square cell accepts square images. Explicit **Landscape** and **Portrait** exclude square images. **Any** accepts all shapes. A filter with no eligible images shows a clear empty state; it never silently substitutes a different orientation.

**Fill** (default) crops the edges to cover the cell, with no letterboxing. **Fit** preserves the whole image with letterboxing. Both preserve the image proportions.

The resolution option compares the source's original dimensions with the physical cell dimensions and selected scale, including when content zoom is enabled:

- **Prefer native size** (default) chooses images needing no enlargement. Smaller images enter the rotation only if none qualify.
- **Native size only** shows an empty state if every matching image would need enlargement.
- **Allow smaller images** rotates through both large and small matching images.

For Fit, the required scale is `min(cell width / image width, cell height / image height)`. For Fill it is `max(...)`. A scale at or below 1 qualifies as native size. Images with unknown dimensions are skipped. The widget requests a Civitai CDN rendition sized to the cell when available. Native modes can use the original when the needed size exceeds the sized CDN rendition. Native size only checks the decoded dimensions before showing an image. Original files can be substantially larger. Allow smaller uses ordinary browser resizing.

## Refresh and rotation

**Prioritize** chooses **Most reactions** (default), **Most collected**, **Most comments** or **Newest**. Civitai ranks the candidate collection using that order and the source time period (Month by default). Rotation then cycles through eligible cached matches. Changing priority fetches a separate collection once; switching back reuses it until its refresh is due. These sorts are supported by [Civitai's image API](https://github.com/civitai/civitai-developer-docs/blob/main/site/reference/images.md).

**Prioritize fresh art** uses Civitai's newest results and prefers more recently posted eligible art within each source priority. It uses the configured collection refresh interval.

The candidate pool refreshes every 24 hours by default. The illustration/painting preset normally needs two image-list requests per refresh and no tag lookups. An empty source can follow one additional cursor, within a total cap of eight image-list requests. Renders and placements with the same source filters share the persistent cache; changing orientation, resolution or engagement minimums does not make another API request. Custom tag-name lookups are cached for 30 days. **More options** contains engagement minimums, collection refresh, image rotation, time period, scale, tag matching and creator credit controls.

Image rotation defaults to 60 minutes. The choice stays stable within each time interval for each placement, including when the collection refreshes or changes order. **More options > Avoid repeats (days)** defaults to seven days. A small persistent history file remembers each placement's scheduled picks across collection refreshes and restarts; editor previews read that history without advancing it. Creator images get priority among picks outside the repeat window. Once all eligible images are recent, the oldest pick is reused. Set the lookback to `0` to disable it.

Civitai rotation also defers artwork from the same post or from the same creator within five minutes when other eligible picks are available. It uses metadata from the existing gallery response and keeps batch details for the last 64 Civitai picks that had them.

Orientation, resolution and engagement filters still apply before rotation. A small eligible pool can repeat sooner than the chosen window. History records the selected primary image when Tesserae renders it; it is not a delivery acknowledgment from the display, and a CDN load failure can use a different cached image. Rotation and collection updates appear on the next Tesserae render; these options do not schedule display refreshes.

If Civitai is temporarily unavailable, a previously cached pool for the identical filter configuration can remain visible. Warm renders reuse the parsed cache and skip the refresh worker. One shared refresh runs at a time, and a render waits at most 4.5 seconds for it before using cached content or showing an updating message. Requests and pagination are capped, with backoff after failures or rate limits. Gallery requests accept gzip and enforce limits on wire and decoded response sizes. The renderer makes up to three image download attempts within a 12-second total loading budget, including any native-resolution retry. Image retries make no gallery API requests.

## Tests

From a Tesserae checkout, copy this repository's `picture_civitai` folder into `plugins/` (a copy, not a symlink), then run the focused backend tests using the host's Python environment:

```sh
.venv/bin/python -m pytest plugins/picture_civitai/tests -n 2
```

The client selection and rendering tests use Node's built-in runner, with no npm dependencies. Run from this repository:

```sh
node --test picture_civitai/tests/test_client.mjs
```

Tests use controlled responses and do not require an API key or live network access.

## License

AGPL-3.0-or-later. See [LICENSE](LICENSE).
