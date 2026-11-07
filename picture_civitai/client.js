function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, character => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[character]);
}

function allowedUrl(value, hostname) {
  try {
    const url = new URL(value);
    return url.protocol === "https:" && url.hostname === hostname
      && !url.username && !url.password && !url.port;
  } catch {
    return false;
  }
}

function shape(width, height) {
  return width > height ? "landscape" : width < height ? "portrait" : "square";
}

// The host may report virtual client dimensions when content zoom is enabled.
export function selectImage(images, cell, options = {}, runtime = {}) {
  const width = Number(cell?.w);
  const height = Number(cell?.h);
  if (![width, height].every(value => Number.isFinite(value) && value > 0)) return null;

  const orientation = ["landscape", "portrait", "any"].includes(options.orientation)
    ? options.orientation : shape(width, height);
  const seen = new Set();
  let pool = (Array.isArray(images) ? images : []).filter(image => {
    if (!image || !allowedUrl(image.url, "image.civitai.com")) return false;
    const w = Number(image.width);
    const h = Number(image.height);
    if (![w, h].every(value => Number.isFinite(value) && value > 0)) return false;
    if (orientation !== "any" && shape(w, h) !== orientation) return false;
    const identity = String(image.id ?? image.url);
    if (seen.has(identity)) return false;
    seen.add(identity);
    return true;
  });
  if (options.resolution !== "allow_smaller") {
    const native = pool.filter(image => {
      const ratios = [width / Number(image.width), height / Number(image.height)];
      const scale = options.scale === "fit" ? Math.min(...ratios) : Math.max(...ratios);
      return scale <= 1;
    });
    if (native.length || options.resolution === "native_only") pool = native;
  }
  if (!pool.length) return null;

  pool.sort((a, b) => {
    const left = String(a.id ?? a.url);
    const right = String(b.id ?? b.url);
    return left < right ? -1 : left > right ? 1 : 0;
  });
  let offset = 0;
  for (const character of String(runtime.placementId ?? "")) {
    offset = (Math.imul(offset, 31) + character.codePointAt(0)) >>> 0;
  }
  const minutes = Number(options.rotation_minutes ?? 60);
  const interval = (Number.isFinite(minutes) && minutes > 0 ? minutes : 60) * 60000;
  const now = Number(runtime.now ?? Date.now());
  const bucket = Math.floor((Number.isFinite(now) ? now : Date.now()) / interval);
  const index = ((offset % pool.length + bucket % pool.length) % pool.length + pool.length) % pool.length;
  return pool[index];
}

function waitForImage(image) {
  return new Promise(resolve => {
    let settled = false;
    const finish = ready => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      image.removeEventListener("load", onLoad);
      image.removeEventListener("error", onError);
      resolve(ready);
    };
    const onError = () => finish(false);
    const onLoad = async () => {
      try {
        if (image.decode) await image.decode();
        finish(image.naturalWidth > 0);
      } catch {
        finish(false);
      }
    };
    const timer = setTimeout(() => finish(false), 12000);
    image.addEventListener("load", onLoad, { once: true });
    image.addEventListener("error", onError, { once: true });
    if (image.complete) {
      if (image.naturalWidth > 0) onLoad();
      else finish(false);
    }
  });
}

export default async function render(shadow, ctx = {}) {
  const data = ctx.data ?? {};
  const options = ctx.cell?.options ?? {};
  const target = [data.target_w, data.target_h].map(Number);
  const cell = target.every(value => Number.isFinite(value) && value > 0)
    ? { w: target[0], h: target[1] } : ctx.cell;
  const image = selectImage(data.images, cell, options, {
    placementId: data.placement_id ?? ctx.placement_id ?? ctx.cell?.id ?? "",
  });
  const css = `<link rel="stylesheet" href="/static/style/spectra-widgets.css">
    <link rel="stylesheet" href="/plugins/picture_civitai/client.css">`;

  if (!image) {
    const hasPool = Array.isArray(data.images) && data.images.length > 0;
    const message = hasPool
      ? "No images match this cell's orientation and resolution. Adjust the image filters."
      : data.error || "No matching Civitai images. Try fewer filters or a longer time period.";
    shadow.innerHTML = `${css}<div class="w is-bleed" data-widget="picture_civitai">
      <div class="bleed-empty">${escapeHtml(message)}</div></div>`;
    return;
  }

  const credit = image.username ? `Civitai / ${image.username}` : "Civitai";
  const permalink = allowedUrl(image.permalink, "civitai.com")
    && /^\/images\/\d+\/?$/.test(new URL(image.permalink).pathname) ? image.permalink : "";
  const creditText = escapeHtml(credit);
  const creditHtml = permalink
    ? `<a href="${escapeHtml(permalink)}" target="_blank" rel="noopener noreferrer">${creditText}</a>`
    : creditText;
  shadow.innerHTML = `${css}
    <div class="w is-bleed" data-widget="picture_civitai">
      <img src="${escapeHtml(image.url)}" alt="${escapeHtml(image.username ? `Artwork by ${image.username}` : "Civitai artwork")}" style="object-fit:${options.scale === "fit" ? "contain" : "cover"};">
      ${options.show_credit !== false ? `<div class="img-overlay"><span class="sub">${creditHtml}</span></div>` : ""}
    </div>`;
  const element = shadow.querySelector?.("img");
  if (element && !await waitForImage(element) && shadow.querySelector("img") === element) {
    shadow.innerHTML = `${css}<div class="w is-bleed" data-widget="picture_civitai">
      <div class="bleed-empty">The Civitai image could not be loaded. Try refreshing this widget.</div></div>`;
  }
}
