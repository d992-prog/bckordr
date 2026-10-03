import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";

const markUrl = new URL("../src/brand/VeltrixMark.tsx", import.meta.url);
const cssUrl = new URL("../src/brand/veltrix-brand.css", import.meta.url);
const assetUrl = new URL("../public/veltrix-mark.svg", import.meta.url);

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
