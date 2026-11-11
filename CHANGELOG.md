# Changelog

## [Unreleased]

- Temporarily disable Lexica and use Civitai for saved Lexica gallery settings.
- Size Civitai downloads to the frame, verify native image dimensions, and reduce API, cache, and render overhead.
- Prefer different Civitai batches during rotation and add a Prioritize fresh art switch.
- Choose tags, creators, and own or followed Civitai collections with source checkboxes and cached collection previews.
- Ask for an exact tag name when a saved API key works but Civitai has no matching tag.
- Combine Civitai and Lexica with shared rotation, separate caches and fallback when a service is unavailable.
- Add Lineups artwork arrows, cached previews and pins that leave rotation history unchanged.
- Add a query preview with matched tags and creators, and a choice of Civitai or Lexica.
- Prefer creator artwork, add tag discoveries, and remember recent picks across collection refreshes.
- Add minimum reaction and comment filters that reuse the cached collection.
- Add Most collected and Most comments priorities alongside Most reactions and Newest.
- Use native-width CDN images and try another cached match when an image fails to load.
- Keep Civitai downloads within Tesserae's declared domain permissions.
- Add rotating Civitai artwork with tag and creator filters, automatic orientation, and resolution preferences.
