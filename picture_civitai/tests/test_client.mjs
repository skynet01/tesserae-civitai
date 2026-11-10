import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const source = await readFile(new URL("../client.js", import.meta.url), "utf8");
const { default: render, selectImage } = await import(
  `data:text/javascript;base64,${Buffer.from(source).toString("base64")}`
);

function image(id, width, height, extra = {}) {
  return {
    id, width, height, url: `https://image.civitai.com/test/${id}.jpeg`,
    username: "Artist", permalink: `https://civitai.com/images/${id}`,
    tags: ["landscape"], ...extra,
  };
}

const landscape = { w: 1600, h: 1200 };
const portrait = { w: 600, h: 900 };

test("a pinned artwork wins over rotation and creator priority", () => {
  const images = [image(1, 2400, 1600, { source_priority: 0 }), image(2, 2400, 1600)];
  assert.equal(selectImage(images, landscape, { pinned_image: "2" }, { selectedId: 1 }).id, 2);
});

test("a pin cannot substitute another image or bypass physical eligibility", () => {
  const images = [image(1, 2400, 1600), image(2, 1800, 2400), image(3, 800, 600)];
  for (const pinned_image of ["2", "3", "999"]) {
    assert.equal(selectImage(images, landscape, { pinned_image }, { selectedId: 1 }), null);
  }
});

test("a failed pinned artwork cannot retry a different cached item", async () => {
  const shadow = imageShadow([false, true]);
  await render(shadow, { cell: { ...landscape, options: { pinned_image: "2" } }, data: {
    images: [image(1, 2400, 1600), image(2, 2400, 1600)], selected_id: 1,
  } });
  assert.deepEqual(shadow.attempts, ["https://image.civitai.com/test/2.jpeg"]);
  assert.match(shadow.innerHTML, /could not be loaded/);
});

test("Lexica artwork renders with its own credit and strict CDN", async () => {
  const artwork = image("lexica:0482ee68-0368-4eca-8846-5930db866b33", 2400, 1600, {
    provider: "lexica", username: "", url: "https://image.lexica.art/md/0482ee68-0368-4eca-8846-5930db866b33",
    permalink: "https://lexica.art/?q=0482ee68-0368-4eca-8846-5930db866b33",
  });
  assert.equal(selectImage([artwork], landscape, {}, { now: 0 }).id, artwork.id);
  const shadow = { innerHTML: "" };
  await render(shadow, { cell: landscape, data: { images: [artwork] } });
  assert.match(shadow.innerHTML, /src="https:\/\/image.lexica.art/);
  assert.match(shadow.innerHTML, />Lexica<\/a>/);
  assert.doesNotMatch(shadow.innerHTML, /Civitai artwork/);
  for (const url of ["https://image.lexica.art.evil.test/a", "http://image.lexica.art/a", "https://image.civitai.com/a"]) {
    assert.equal(selectImage([{...artwork, url}], landscape), null);
  }
});

test("auto orientation follows the cell even on an oppositely oriented panel", () => {
  const images = [image(1, 2400, 1600), image(2, 1200, 1800)];
  assert.equal(selectImage(images, portrait, {}, { now: 0 }).id, 2);
  const shadow = { innerHTML: "" };
  render(shadow, { cell: portrait, panel: landscape, data: { images } });
  assert.match(shadow.innerHTML, /src="https:\/\/image\.civitai\.com\/test\/2.jpeg"/);
});

test("orientation stays strict and square auto cells require square images", () => {
  const images = [image(1, 2400, 1600), image(2, 1800, 1800)];
  assert.equal(selectImage(images, portrait, {}, { now: 0 }), null);
  assert.equal(selectImage(images, { w: 800, h: 800 }, {}, { now: 0 }).id, 2);
  assert.equal(selectImage(images, portrait, { orientation: "any" }, { now: 0 })?.id, 1);
  assert.equal(selectImage(images, portrait, { orientation: "landscape" }, { now: 0 })?.id, 1);
});

test("fit and fill assess native resolution using their actual scaling factors", () => {
  const images = [image(1, 1800, 1000)];
  assert.equal(selectImage(images, landscape, { resolution: "native_only", scale: "fit" }, { now: 0 })?.id, 1);
  assert.equal(selectImage(images, landscape, { resolution: "native_only", scale: "fill" }, { now: 0 }), null);
  assert.equal(selectImage([image(2, 1600, 1200)], landscape, { resolution: "native_only", scale: "fill" }, { now: 0 })?.id, 2);
});

test("preferred resolution exhausts native candidates before using smaller images", () => {
  const images = [image(1, 800, 600), image(2, 2000, 1500)];
  for (let bucket = 0; bucket < 4; bucket++) {
    assert.equal(selectImage(images, landscape, {}, { now: bucket * 3600000 }).id, 2);
  }
  assert.equal(selectImage(images.slice(0, 1), landscape, {}, { now: 0 }).id, 1);
  assert.equal(selectImage(images.slice(0, 1), landscape, { resolution: "native_only" }, { now: 0 }), null);
  assert.deepEqual([0, 3600000].map(now => selectImage(images, landscape, { resolution: "allow_smaller" }, { now }).id), [1, 2]);
});

test("rotation is stable within an interval and visits every image before repeating", () => {
  const images = [image(1, 2400, 1600), image(2, 2400, 1600), image(3, 2400, 1600)];
  const options = { rotation_minutes: 30 };
  const runtime = { placementId: "page:cell:device", now: 1 };
  const first = selectImage(images, landscape, options, runtime).id;
  assert.equal(selectImage(images, landscape, options, { ...runtime, now: 1799999 }).id, first);
  const ids = [1, 1800000, 3600000, 5400000].map(now => selectImage(images, landscape, options, { ...runtime, now }).id);
  assert.equal(new Set(ids.slice(0, 3)).size, 3);
  assert.equal(ids[0], ids[3]);
  assert.equal(selectImage([...images].reverse(), landscape, options, runtime).id, first);
  assert.notEqual(selectImage(images, landscape, options, { now: 1, placementId: "another" }).id, first);
});

test("unsafe URLs, missing dimensions and duplicate images do not enter rotation", () => {
  const bad = [
    image(1, 2400, 1600, { url: "https://evil.example/image.jpg" }),
    image(2, 2400, 1600, { url: "https://image.civitai.com.evil.example/a" }),
    image(3, 2400, 1600, { url: "http://image.civitai.com/a" }),
    image(4, 2400, 1600, { url: "https://user@image.civitai.com/a" }),
    image(5, 2400, 1600, { url: "https://image.civitai.com:444/a" }),
    image(6, 0, 1600), image(7, null, 1600), image(8, Infinity, 1600),
  ];
  assert.equal(selectImage(bad, landscape, { resolution: "allow_smaller" }, { now: 0 }), null);
  assert.equal(selectImage([null, ...bad, image(9, 2400, 1600)], landscape, {}, { now: 0 }).id, 9);
  const duplicate = image(1, 2400, 1600);
  const pool = [duplicate, duplicate, image(2, 2400, 1600)];
  assert.deepEqual([0, 3600000].map(now => selectImage(pool, landscape, {}, { now }).id), [1, 2]);
  assert.equal(selectImage([duplicate], { w: 0, h: 500 }, {}, { now: 0 }), null);
});

test("render escapes public text and attributes, and rejects arbitrary credit links", () => {
  const shadow = { innerHTML: "" };
  render(shadow, {
    cell: landscape,
    data: { images: [image(1, 2400, 1600, {
      username: '\"><script>alert(1)</script>',
      url: 'https://image.civitai.com/test/1.jpeg?x="onerror="alert(1)',
      permalink: 'https://evil.example/"onclick="alert(1)',
    })] },
  });
  assert.doesNotMatch(shadow.innerHTML, /<script>|href="https:\/\/evil|"onerror="/);
  assert.match(shadow.innerHTML, /&lt;script&gt;/);
  assert.match(shadow.innerHTML, /&quot;onerror=/);
  render(shadow, { cell: landscape, data: { error: '<script>broken</script>' } });
  assert.doesNotMatch(shadow.innerHTML, /<script>/);
  assert.match(shadow.innerHTML, /&lt;script&gt;broken/);
});

test("stale usable images remain visible and credit can be hidden", () => {
  const shadow = { innerHTML: "" };
  render(shadow, {
    cell: { ...landscape, options: { show_credit: false, scale: "fill" } },
    data: { stale: true, error: "Temporary upstream failure", images: [image(1, 2400, 1600)] },
  });
  assert.match(shadow.innerHTML, /<img /);
  assert.match(shadow.innerHTML, /object-fit:cover/);
  assert.doesNotMatch(shadow.innerHTML, /img-overlay|Temporary upstream failure/);
});

test("native-only exhaustion renders an honest empty state without a wrong image", () => {
  const shadow = { innerHTML: "" };
  render(shadow, {
    cell: { ...landscape, options: { resolution: "native_only" } },
    data: { images: [image(1, 800, 600)] },
  });
  assert.doesNotMatch(shadow.innerHTML, /<img /);
  assert.match(shadow.innerHTML, /No images match/);
});

test("a failed image load shows an explanation instead of a broken image", async () => {
  const element = { complete: true, naturalWidth: 0, addEventListener() {}, removeEventListener() {} };
  const shadow = { innerHTML: "", querySelector: () => element };
  await render(shadow, { cell: landscape, data: { images: [image(1, 2400, 1600)] } });
  assert.doesNotMatch(shadow.innerHTML, /<img /);
  assert.match(shadow.innerHTML, /could not be loaded/);
});

test("render waits for the chosen image before completing", async () => {
  let onLoad;
  let finished = false;
  const element = {
    complete: false, naturalWidth: 0,
    addEventListener(event, callback) { if (event === "load") onLoad = callback; },
    removeEventListener() {},
    async decode() {},
  };
  const shadow = { innerHTML: "", querySelector: () => element };
  const pending = Promise.resolve(render(shadow, { cell: landscape, data: { images: [image(1, 2400, 1600)] } }))
    .then(() => { finished = true; });
  await Promise.resolve();
  assert.equal(finished, false);
  element.naturalWidth = 2400;
  onLoad();
  await pending;
  assert.match(shadow.innerHTML, /<img /);
});

test("native resolution follows physical cell pixels when content zoom is enabled", async () => {
  const shadow = { innerHTML: "" };
  await render(shadow, {
    cell: { w: 500, h: 400, options: { resolution: "native_only", scale: "fill" } },
    data: { target_w: 1000, target_h: 800, images: [image(1, 800, 600)] },
  });
  assert.doesNotMatch(shadow.innerHTML, /<img /);
  await render(shadow, {
    cell: { w: 800, h: 600, options: { resolution: "native_only", scale: "fill" } },
    data: { target_w: 400, target_h: 300, images: [image(2, 400, 300)] },
  });
  assert.match(shadow.innerHTML, /<img /);
});

test("the default fills the cell and assesses resolution for the full image crop", async () => {
  assert.equal(selectImage([image(1, 1800, 1000)], landscape,
    { resolution: "native_only" }, { now: 0 }), null);
  const shadow = { innerHTML: "" };
  await render(shadow, { cell: landscape, data: { images: [image(2, 2400, 1600)] } });
  assert.match(shadow.innerHTML, /object-fit:cover/);
});

function imageShadow(outcomes) {
  let html = "";
  let current;
  const attempts = [];
  return {
    attempts,
    get innerHTML() { return html; },
    set innerHTML(value) {
      html = value;
      const src = value.match(/<img src="([^"]+)"/);
      if (!src) { current = undefined; return; }
      const loaded = outcomes[attempts.length] === true;
      attempts.push(src[1]);
      current = { complete: true, naturalWidth: loaded ? 2400 : 0,
        addEventListener() {}, removeEventListener() {}, async decode() {} };
    },
    querySelector() { return current; },
  };
}

test("original CDN URLs use the actual source width without requesting an upscale", async () => {
  const shadow = imageShadow([true]);
  await render(shadow, { cell: landscape, data: { images: [image(1, 1024, 680, {
    url: "https://image.civitai.com/token/asset/original=true/painting.png",
  })] } });
  assert.deepEqual(shadow.attempts, ["https://image.civitai.com/token/asset/width=1024/painting.png"]);
});

test("a failed cached image retries another eligible image and keeps a successful result", async () => {
  const shadow = imageShadow([false, true]);
  await render(shadow, { cell: landscape, data: { images: [image(1, 2400, 1600), image(2, 2400, 1600)] } });
  assert.equal(shadow.attempts.length, 2);
  assert.equal(new Set(shadow.attempts).size, 2);
  assert.match(shadow.innerHTML, /<img /);
  assert.doesNotMatch(shadow.innerHTML, /could not be loaded/);
});

test("three failed cached images stop with a friendly load failure", async () => {
  const shadow = imageShadow([false, false, false, true]);
  await render(shadow, { cell: landscape, data: {
    images: [1, 2, 3, 4].map(id => image(id, 2400, 1600)),
  } });
  assert.equal(shadow.attempts.length, 3);
  assert.equal(new Set(shadow.attempts).size, 3);
  assert.doesNotMatch(shadow.innerHTML, /<img /);
  assert.match(shadow.innerHTML, /could not be loaded/);
});

test("load fallback cannot choose a wrong orientation or below-native image", async () => {
  const shadow = imageShadow([false, true]);
  await render(shadow, {
    cell: { ...landscape, options: { resolution: "native_only" } },
    data: { images: [image(1, 2400, 1600), image(2, 1800, 2400), image(3, 800, 600)] },
  });
  assert.deepEqual(shadow.attempts, ["https://image.civitai.com/test/1.jpeg"]);
  assert.match(shadow.innerHTML, /could not be loaded/);
});

test("an old failed render cannot retry over a fresh successful render", async () => {
  let failOld;
  let html = "";
  let current;
  const attempts = [];
  const shadow = {
    get innerHTML() { return html; },
    set innerHTML(value) {
      html = value;
      const src = value.match(/<img src="([^"]+)"/);
      if (!src) { current = undefined; return; }
      attempts.push(src[1]);
      const old = attempts.length === 1;
      current = { complete: !old, naturalWidth: old ? 0 : 2400,
        addEventListener(event, callback) { if (old && event === "error") failOld = callback; },
        removeEventListener() {}, async decode() {} };
    },
    querySelector() { return current; },
  };
  const old = render(shadow, { cell: landscape, data: { images: [image(1, 2400, 1600), image(2, 2400, 1600)] } });
  await render(shadow, { cell: landscape, data: { images: [image(99, 2400, 1600)] } });
  failOld();
  await old;
  assert.equal(attempts.length, 2);
  assert.match(shadow.innerHTML, /test\/99.jpeg/);
});

test("three slow attempts share a total twelve-second load budget", async t => {
  let elapsed = 0;
  const timers = [];
  const delays = [];
  t.mock.method(Date, "now", () => elapsed);
  t.mock.method(globalThis, "setTimeout", (callback, delay) => {
    timers.push(callback);
    delays.push(delay);
    return delays.length;
  });
  t.mock.method(globalThis, "clearTimeout", () => {});
  let html = "";
  let current;
  const shadow = {
    get innerHTML() { return html; },
    set innerHTML(value) {
      html = value;
      current = value.includes("<img ")
        ? { complete: false, naturalWidth: 0, addEventListener() {}, removeEventListener() {} }
        : undefined;
    },
    querySelector() { return current; },
  };
  const pending = render(shadow, { cell: landscape, data: {
    images: [1, 2, 3, 4].map(id => image(id, 2400, 1600)),
  } });
  for (let attempt = 0; attempt < 3 && timers.length; attempt++) {
    elapsed += delays[attempt];
    timers.shift()();
    await new Promise(setImmediate);
  }
  await pending;
  assert.deepEqual(delays, [4000, 4000, 4000]);
  assert.equal(elapsed, 12000);
  assert.match(shadow.innerHTML, /could not be loaded/);
});

test("a valid durable selected ID anchors the rendered image", async () => {
  const images = [image(1, 2400, 1600), image(2, 2400, 1600)];
  assert.equal(selectImage(images, landscape, {}, { now: 0, selectedId: 2 }).id, 2);
  const shadow = imageShadow([true]);
  await render(shadow, { cell: landscape, data: { images, selected_id: 2 } });
  assert.deepEqual(shadow.attempts, ["https://image.civitai.com/test/2.jpeg"]);
});

test("a durable selected ID cannot bypass orientation or preferred native resolution", () => {
  const images = [image(1, 2400, 1600), image(2, 1800, 2400), image(3, 800, 600)];
  for (const selectedId of [2, 3, 999]) {
    assert.equal(selectImage(images, landscape, {}, { now: 0, selectedId }).id, 1);
  }
  assert.equal(selectImage(images.slice(2), landscape, { resolution: "native_only" }, { now: 0, selectedId: 3 }), null);
});

test("eligible creator images take priority over tag and legacy candidates", () => {
  const images = [image(1, 2400, 1600), image(2, 2400, 1600, { source_priority: 1 }),
    image(3, 2400, 1600, { source_priority: 0 })];
  for (let bucket = 0; bucket < 4; bucket++) {
    assert.equal(selectImage(images, landscape, {}, { now: bucket * 3600000 }).id, 3);
  }
  assert.equal(selectImage([image(1, 800, 600, { source_priority: 0 }), image(2, 2400, 1600)],
    landscape, {}, { now: 0 }).id, 2);
});

test("a valid durable tag anchor can override creator preference to avoid a recent repeat", () => {
  const images = [image(1, 2400, 1600, { source_priority: 0 }), image(2, 2400, 1600, { source_priority: 1 })];
  assert.equal(selectImage(images, landscape, {}, { now: 0, selectedId: 2 }).id, 2);
});

test("a failed creator anchor retries another eligible creator before a tag image", async () => {
  const shadow = imageShadow([false, true]);
  const images = [image(1, 2400, 1600, { source_priority: 1 }),
    image(2, 2400, 1600, { source_priority: 0 }), image(3, 2400, 1600, { source_priority: 0 })];
  await render(shadow, { cell: landscape, data: { images, selected_id: 2 } });
  assert.deepEqual(shadow.attempts, ["https://image.civitai.com/test/2.jpeg", "https://image.civitai.com/test/3.jpeg"]);
});
