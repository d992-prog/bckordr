import test from "node:test";
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { access, readdir, readFile } from "node:fs/promises";

const markUrl = new URL("../src/brand/VeltrixMark.tsx", import.meta.url);
const cssUrl = new URL("../src/brand/veltrix-brand.css", import.meta.url);
const assetUrl = new URL("../public/veltrix-mark.svg", import.meta.url);
const rawAssetRoot = new URL("../../.impeccable/assets/", import.meta.url);
const provenanceRoot = new URL("../../.impeccable/provenance/brand/", import.meta.url);
const shippingAssetRoot = new URL("../public/brand/", import.meta.url);
const repositoryRoot = new URL("../../", import.meta.url);
const measuredSpecUrl = new URL("../../.impeccable/build/spec.json", import.meta.url);

function luminance(hex) {
  const channels = hex
    .slice(1)
    .match(/.{2}/g)
    .map((channel) => Number.parseInt(channel, 16) / 255)
    .map((channel) =>
      channel <= 0.04045 ? channel / 12.92 : ((channel + 0.055) / 1.055) ** 2.4,
    );
  return channels[0] * 0.2126 + channels[1] * 0.7152 + channels[2] * 0.0722;
}

function contrast(foreground, background) {
  const [lighter, darker] = [luminance(foreground), luminance(background)].sort((a, b) => b - a);
  return (lighter + 0.05) / (darker + 0.05);
}

function token(css, name) {
  return css.match(new RegExp(`${name}:\\s*(#[0-9a-f]{6})`, "i"))?.[1];
}

function pngHeader(buffer) {
  assert.equal(buffer.subarray(1, 4).toString("ascii"), "PNG");
  return {
    width: buffer.readUInt32BE(16),
    height: buffer.readUInt32BE(20),
    colorType: buffer[25],
  };
}

test("shared Veltrix brand is vector, accessible, and tokenized", async () => {
  const [mark, css, asset] = await Promise.all([
    readFile(markUrl, "utf8"),
    readFile(cssUrl, "utf8"),
    readFile(assetUrl, "utf8"),
  ]);

  assert.match(mark, /aria-hidden=\{decorative\}/);
  assert.match(mark, /Veltrix VPN/);
  for (const token of ["--vx-pearl", "--vx-ink", "--vx-blue", "--vx-mint", "--vx-lilac"]) {
    assert.ok(css.includes(token), token);
  }
  assert.match(css, /prefers-reduced-motion/);
  assert.match(css, /@supports not \(backdrop-filter:/);
  assert.doesNotMatch(asset, />V<|shield|padlock|globe/i);
});

test("Veltrix mark exposes exactly one accessible name in every mode", async () => {
  const mark = await readFile(markUrl, "utf8");

  assert.doesNotMatch(mark, /aria-label=/);
  assert.match(mark, /const markIsDecorative = decorative \|\| withName/);
  assert.match(mark, /aria-hidden=\{decorative\}/);
  assert.match(mark, /aria-hidden=\{markIsDecorative\}/);
  assert.match(mark, /role=\{markIsDecorative \? undefined : "img"\}/);
  assert.match(mark, /!markIsDecorative && <title>Veltrix VPN<\/title>/);
  assert.match(mark, /withName && <span className="vx-brand__name">Veltrix VPN<\/span>/);
});

test("dark brand tokens keep text and focus visible without muddy lens blending", async () => {
  const css = await readFile(cssUrl, "utf8");
  const darkStart = css.indexOf("@media (prefers-color-scheme: dark)");
  const reducedMotionStart = css.indexOf("@media (prefers-reduced-motion: reduce)");

  assert.ok(darkStart >= 0, "dark media query");
  assert.ok(reducedMotionStart > darkStart, "dark tokens precede reduced motion rules");
  const darkCss = css.slice(darkStart, reducedMotionStart);
  const darkGround = token(darkCss, "--vx-pearl");
  const darkInk = token(darkCss, "--vx-ink");
  const darkFocus = token(darkCss, "--vx-focus");

  assert.ok(contrast(darkInk, darkGround) >= 4.5, "dark ink contrast");
  assert.ok(contrast(darkFocus, darkGround) >= 3, "dark focus contrast");
  assert.match(css, /\.vx-brand\s*\{[^}]*isolation:\s*isolate/s);
  assert.match(darkCss, /\.vx-mark__lens\s*\{[^}]*mix-blend-mode:\s*screen/s);
  const darkAtmosphere = darkCss.match(/\.vx-atmosphere\s*\{([^}]*)\}/s)?.[1];
  assert.ok(darkAtmosphere, "dark atmosphere override");
  assert.match(darkAtmosphere, /radial-gradient[\s\S]*linear-gradient/);
  assert.doesNotMatch(darkAtmosphere, /url\(/);
});

test("reduced motion and atmosphere are scoped, responsive, and honest", async () => {
  const [css, atmosphereMeta, shippingMeta, shippingAsset] = await Promise.all([
    readFile(cssUrl, "utf8"),
    readFile(new URL("atmosphere.json", rawAssetRoot), "utf8").then(JSON.parse),
    readFile(new URL("atmosphere.webp.json", provenanceRoot), "utf8").then(JSON.parse),
    readFile(new URL("atmosphere.webp", shippingAssetRoot)),
  ]);
  const reducedMotionCss = css.slice(css.indexOf("@media (prefers-reduced-motion: reduce)"));

  assert.match(reducedMotionCss, /\.vx-motion/);
  assert.doesNotMatch(reducedMotionCss, /^\s*\*/m);
  assert.match(css, /\.vx-atmosphere\s*\{[^}]*radial-gradient[^}]*linear-gradient/s);
  assert.match(css, /url\("\/brand\/atmosphere\.webp"\)[^;]*no-repeat/);
  assert.match(css, /background-size:\s*cover,\s*cover,\s*min\(512px, 100vw\) auto/);
  assert.equal(atmosphereMeta.seamless, false);
  assert.match(atmosphereMeta.usage, /non-repeating/i);
  assert.equal(shippingMeta.seamless, false);
  assert.match(shippingMeta.usage, /non-repeating/i);
  assert.ok(shippingMeta.dimensions.width <= atmosphereMeta.dimensions.width);
  assert.ok(shippingMeta.dimensions.height <= atmosphereMeta.dimensions.height);
  assert.ok(shippingAsset.length > 1_000);
});

test("shipping raster provenance stays complete and outside the browser bundle", async () => {
  const publicFiles = await readdir(shippingAssetRoot);
  assert.deepEqual(publicFiles.sort(), [
    "atmosphere.webp",
    "brand-mark.webp",
    "signal-orb.webp",
    "status-lens.webp",
  ]);

  for (const id of ["atmosphere", "brand-mark", "signal-orb", "status-lens"]) {
    const [metadata, asset] = await Promise.all([
      readFile(new URL(`${id}.webp.json`, provenanceRoot), "utf8").then(JSON.parse),
      readFile(new URL(`${id}.webp`, shippingAssetRoot)),
    ]);
    assert.equal(metadata.regionId, id);
    assert.equal(metadata.asset, `${id}.webp`);
    assert.match(metadata.sourceTool, /image_gen/);
    assert.equal(typeof metadata.sourceModel, "string");
    assert.match(metadata.prompt, /\S/);
    assert.match(metadata.sha256, /^[a-f0-9]{64}$/);
    assert.equal(createHash("sha256").update(asset).digest("hex"), metadata.sha256);
    assert.ok(metadata.dimensions.width > 0);
    assert.ok(metadata.dimensions.height > 0);
    assert.equal(typeof metadata.alpha, "boolean");
    assert.equal(metadata.approved, true);
  }
});

test("every measured raster plate remains available at its recorded contract path", async () => {
  const spec = JSON.parse(await readFile(measuredSpecUrl, "utf8"));
  const plates = spec.regions
    .map((region) => region.plate)
    .filter((plate) => typeof plate === "string");

  assert.ok(plates.length > 0);
  await Promise.all(plates.map((plate) => access(new URL(plate, repositoryRoot))));
});

test("generated plate metadata matches PNG dimensions and alpha intent", async () => {
  for (const expected of [
    { id: "atmosphere", width: 710, height: 1536, colorType: 2, background: "opaque" },
    { id: "brand-mark", width: 1114, height: 1199, colorType: 6, background: "transparent" },
    { id: "status-lens", width: 1631, height: 964, colorType: 6, background: "transparent" },
    { id: "signal-orb", width: 1254, height: 1185, colorType: 6, background: "transparent" },
  ]) {
    const [image, meta] = await Promise.all([
      readFile(new URL(`${expected.id}.png`, rawAssetRoot)),
      readFile(new URL(`${expected.id}.json`, rawAssetRoot), "utf8").then(JSON.parse),
    ]);

    assert.deepEqual(pngHeader(image), {
      width: expected.width,
      height: expected.height,
      colorType: expected.colorType,
    });
    assert.deepEqual(meta.dimensions, { width: expected.width, height: expected.height });
    assert.equal(meta.background, expected.background);
    assert.equal(meta.approved, true);
  }
});

test("runtime typography distinguishes the measured display match from its system fallback", async () => {
  const css = await readFile(cssUrl, "utf8");

  assert.match(css, /Moderustic is the measured match when locally available; otherwise use the system fallback\./);
  assert.match(css, /--vx-font-display:\s*"Moderustic",[^;]*"Segoe UI Variable Display"/);
  assert.match(css, /--vx-hero-title-size:\s*56px/);
  assert.match(css, /--vx-hero-weight:\s*700/);
});
