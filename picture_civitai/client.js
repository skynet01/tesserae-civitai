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

function source(image) {
  return image?.provider === "lexica"
    ? { name: "Lexica", imageHost: "image.lexica.art", host: "lexica.art" }
    : { name: "Civitai", imageHost: "image.civitai.com", host: "civitai.com" };
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
    if (!image || !allowedUrl(image.url, source(image).imageHost)) return false;
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

  // A durable server choice may deliberately rest a recently shown creator.
  // Honour it only after the same orientation and resolution checks as usual.
  if (runtime.selectedId != null) {
    const selected = pool.find(image => String(image.id) === String(runtime.selectedId));
    if (selected) return selected;
  }
  const creators = pool.filter(image => image.source_priority === 0);
  if (creators.length) pool = creators;

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

function waitForImage(image, timeout) {
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
    const timer = setTimeout(() => finish(false), timeout);
    image.addEventListener("load", onLoad, { once: true });
    image.addEventListener("error", onError, { once: true });
    if (image.complete) {
      if (image.naturalWidth > 0) onLoad();
      else finish(false);
    }
  });
}

function imageUrl(image) {
  if (!allowedUrl(image.url, source(image).imageHost)) return "";
  if (image.provider === "lexica") return image.url;
  const url = new URL(image.url);
  if (!url.pathname.includes("/original=true/")) return image.url;
  url.pathname = url.pathname.replace("/original=true/", `/width=${Math.round(Number(image.width))}/`);
  return url.href;
}

const activeRenders = new WeakMap();

export default async function render(shadow, ctx = {}) {
  const token = {};
  activeRenders.set(shadow, token);
  const started = Date.now();
  const data = ctx.data ?? {};
  const options = ctx.cell?.options ?? {};
  const target = [data.target_w, data.target_h].map(Number);
  const cell = target.every(value => Number.isFinite(value) && value > 0)
    ? { w: target[0], h: target[1] } : ctx.cell;
  const runtime = {
    placementId: data.placement_id ?? ctx.placement_id ?? ctx.cell?.id ?? "",
    now: started,
    selectedId: data.selected_id,
  };
  let pool = Array.isArray(data.images) ? data.images : [];
  let image = selectImage(pool, cell, options, runtime);
  const css = `<link rel="stylesheet" href="/static/style/spectra-widgets.css">
    <link rel="stylesheet" href="/plugins/picture_civitai/client.css">`;

  if (!image) {
    const hasPool = Array.isArray(data.images) && data.images.length > 0;
    const message = hasPool
      ? "No images match this cell's orientation and resolution. Adjust the image filters."
      : data.error || "No matching artwork. Try fewer filters or a different search.";
    shadow.innerHTML = `${css}<div class="w is-bleed" data-widget="picture_civitai">
      <div class="bleed-empty">${escapeHtml(message)}</div></div>`;
    return;
  }

  for (let attempt = 0; image && attempt < 3; attempt++) {
    const remaining = 12000 - (Date.now() - started);
    if (remaining <= 0) break;
    const provider = source(image);
    const credit = image.username ? `${provider.name} / ${image.username}` : provider.name;
    const permalink = allowedUrl(image.permalink, provider.host)
      && (image.provider === "lexica"
        ? /^\/?$/.test(new URL(image.permalink).pathname) && /^[0-9a-f-]{36}$/.test(new URL(image.permalink).searchParams.get("q") || "")
        : /^\/images\/\d+\/?$/.test(new URL(image.permalink).pathname)) ? image.permalink : "";
    const creditText = escapeHtml(credit);
    const creditHtml = permalink
      ? `<a href="${escapeHtml(permalink)}" target="_blank" rel="noopener noreferrer">${creditText}</a>`
      : creditText;
    shadow.innerHTML = `${css}
      <div class="w is-bleed" data-widget="picture_civitai">
        <img src="${escapeHtml(imageUrl(image))}" alt="${escapeHtml(image.username ? `Artwork by ${image.username}` : `${provider.name} artwork`)}" style="object-fit:${options.scale === "fit" ? "contain" : "cover"};">
        ${options.show_credit !== false ? `<div class="img-overlay"><span class="sub">${creditHtml}</span></div>` : ""}
      </div>`;
    const element = shadow.querySelector?.("img");
    if (!element) return;
    const loaded = await waitForImage(element, Math.max(0, Math.min(4000, 12000 - (Date.now() - started))));
    if (activeRenders.get(shadow) !== token || shadow.querySelector("img") !== element) return;
    if (loaded) return;
    const failed = String(image.id ?? image.url);
    pool = pool.filter(candidate => candidate && String(candidate.id ?? candidate.url) !== failed);
    image = selectImage(pool, cell, options, runtime);
  }
  shadow.innerHTML = `${css}<div class="w is-bleed" data-widget="picture_civitai">
    <div class="bleed-empty">The artwork could not be loaded. Try refreshing this widget.</div></div>`;
}
