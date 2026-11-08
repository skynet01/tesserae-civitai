# Civitai picture widget for Tesserae

Rotating artwork from [Civitai](https://civitai.com) for [Tesserae](https://github.com/dmellok/tesserae). Images fill the widget area, with an optional quiet creator credit. Configuration uses Tesserae's normal widget settings and cell options.

![Painted artwork on a portrait Tesserae cell](screenshots/lg.png)

Preview artwork: [jankytrees on Civitai](https://civitai.com/images/147746839).

Requires Tesserae 0.484.1 or newer. Folder shipped: `picture_civitai`.

## Install

Install through **Settings > Widgets > Browse community widgets** when this repository is listed in the catalog. For a manual installation, copy the `picture_civitai` folder into your Tesserae checkout's `plugins/` folder and restart Tesserae. Keep the folder name: it is the widget ID.

Add **Picture, Civitai** as the only widget on a full-display page for a full-screen art frame. It starts with edge-to-edge **Fill** scaling and automatic orientation, showing **drawing, illustration or painting**, excluding **photography and photorealistic** tags, sorted by most reactions for the month. Leaving tags and creators blank uses the general public SFW image feed.

## Filters

- **Include tags:** comma- or newline-separated names or numeric tag IDs. Images match any included tag by default; **More options > Include tag matching > All tags** requires every tag.
- **Creators:** comma- or newline-separated Civitai usernames. This restricts the candidate pool to those creators; tag filters then apply within their images.
- **Exclude tags:** every exclusion applies. An image with any excluded tag is rejected, including when that tag is also included. Names and numeric IDs are supported.

Creator filters, negative tag names and the default drawing/illustration/painting preset work without an API key. The preset's verified image tag IDs are built in, so it needs no tag lookup requests. Without a key, other positive tag names are resolved from a sample of the popular public feed; names absent from that sample produce a message asking for an API key or numeric tag ID. This sample is not Civitai's complete tag index.

For full positive tag-name lookup, create a free personal API key at [Civitai account settings](https://civitai.com/user/account) and save it in **Settings > Plugins > Picture, Civitai > Civitai API key**. Tesserae stores it as a secret and does not send it to the widget client. The widget uses Civitai's public image API and an internal tRPC image-tag lookup endpoint. The internal endpoint requires authentication and may change independently of the public API.

Only public images marked SFW by Civitai are requested. Filtering depends on Civitai's metadata; it does not inspect image contents. Candidate collection is bounded, so narrow combinations can have no matches even when matching images exist elsewhere on Civitai.

The manifest declares `network:*` because Tesserae's checked downloader connects to pinned public IP addresses, which its domain-only capability declarations reject. The widget still accepts only the two fixed HTTPS API paths on `civitai.com`, rejects redirects outside them, and validates image URLs against `image.civitai.com`. Private-address and DNS-rebinding protection remain active.

## Orientation and image size

**Auto** uses the actual cell dimensions, even when the cell and whole display have different orientations. A landscape cell accepts landscape images, a portrait cell accepts portrait images, and a square cell accepts square images. Explicit **Landscape** and **Portrait** exclude square images. **Any** accepts all shapes. A filter with no eligible images shows a clear empty state; it never silently substitutes a different orientation.

**Fill** (default) crops the edges to cover the cell, with no letterboxing. **Fit** preserves the whole image with letterboxing. Both preserve the image proportions.

The resolution option compares the source's original dimensions with the physical cell dimensions and selected scale, including when content zoom is enabled:

- **Prefer native size** (default) chooses images needing no enlargement. Smaller images enter the rotation only if none qualify.
- **Native size only** shows an empty state if every matching image would need enlargement.
- **Allow smaller images** rotates through both large and small matching images.

For Fit, the required scale is `min(cell width / image width, cell height / image height)`. For Fill it is `max(...)`. A scale at or below 1 qualifies as native size. Images with unknown dimensions are skipped. The client uses original image URLs and ordinary browser resizing, with no AI upscaling or added detail.

## Refresh and rotation

The candidate pool refreshes every 24 hours by default. The drawing/illustration/painting preset normally needs three image-list requests per refresh and no tag lookups. An empty source can follow one additional cursor, within a total cap of eight image-list requests. Renders and placements with the same filters share the persistent cache; changing orientation or resolution does not make another API request. Custom tag-name lookups are cached for 30 days. **More options** contains collection refresh, image rotation, source order, time period, scale, tag matching and creator credit controls.

Image rotation defaults to 60 minutes. The choice stays stable within each time interval for each placement and advances through the eligible pool without repeating until it has visited every image. Pool changes can change that sequence. Rotation and collection updates appear on the next Tesserae render; these options do not schedule display refreshes.

If Civitai is temporarily unavailable, a previously cached pool for the identical filter configuration can remain visible. One shared refresh runs at a time, and a render waits at most 4.5 seconds before using cached content or showing an updating message. Requests and pagination are capped, with backoff after failures or rate limits. Candidate metadata is cached; the selected image is loaded from Civitai's image CDN by the renderer. The image renderer waits up to 12 seconds for loading and decoding, then shows a load-failure message if needed.

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
