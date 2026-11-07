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
