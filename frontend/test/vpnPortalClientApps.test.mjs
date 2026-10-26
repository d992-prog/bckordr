import assert from "node:assert/strict";
import test from "node:test";

const apps = await import("../src/vpn-portal/clientApps.ts").catch((error) => {
  if (error.code === "ERR_MODULE_NOT_FOUND") return {};
  throw error;
});

test("detects all device classes including an iPad using a desktop user agent", () => {
  assert.equal(typeof apps.detectClientPlatform, "function");
  for (const [ua, expected] of [
    ["Mozilla iPhone CPU iPhone OS 17_6", "iphone"],
    ["Mozilla Android 15 Linux", "android"],
    ["Mozilla Windows NT 10.0", "windows"],
    ["Mozilla Macintosh Mac OS X", "macos"],
    ["Mozilla X11 Linux x86_64", "linux"],
  ]) assert.equal(apps.detectClientPlatform(ua), expected);
  assert.equal(apps.detectClientPlatform("Mozilla Macintosh Mac OS X", 5), "iphone");
  assert.equal(apps.detectClientPlatform(""), "iphone");
});

test("builds allowlisted import schemes and separately encodes the profile fragment", () => {
  assert.equal(typeof apps.buildAppImportUrl, "function");
  const uri = "https://vpn.example/api/vpn-portal/subscription/secret?format=auto&v=1";
  assert.equal(apps.buildAppImportUrl("iphone", uri, "Veltrix / Мой #1"), `happ://add/${uri}#Veltrix%20%2F%20%D0%9C%D0%BE%D0%B9%20%231`);
  assert.equal(apps.buildAppImportUrl("windows", uri, "Veltrix VPN"), `hiddify://import/${uri}#Veltrix%20VPN`);
  assert.equal(apps.buildAppImportUrl("linux", uri, "VPN"), `hiddify://import/${uri}#VPN`);
  assert.throws(() => apps.buildAppImportUrl("other", uri, "VPN"), /platform/);
});

test("rejects direct keys, unsafe URLs and fragments without leaking the secret", () => {
  assert.equal(typeof apps.buildAppImportUrl, "function");
  for (const uri of ["vless://private", "http://vpn.example/private", "javascript:private", "https://user:private@vpn.example/sub", "https://vpn.example/sub#private", " https://vpn.example/sub", "https://vpn.example/\nprivate"]) {
    assert.throws(() => apps.buildAppImportUrl("iphone", uri, "VPN"), (error) => !error.message.includes("private") && /subscription/.test(error.message));
  }
});
