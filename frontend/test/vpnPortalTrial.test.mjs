import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";

import * as trialPolling from "../src/vpn-portal/trialPolling.ts";

const { nextTrialPoll } = trialPolling;

const trialCardUrl = new URL("../src/vpn-portal/TrialCard.tsx", import.meta.url);
const portalUrl = new URL("../src/vpn-portal/Portal.tsx", import.meta.url);
const cssUrl = new URL("../src/vpn-portal/portal.css", import.meta.url);

test("trial card renders every public state without internal VPN fields", async () => {
  const source = await readFile(trialCardUrl, "utf8");

  for (const state of ["disabled", "available", "capacity_paused", "preparing", "active", "used"]) {
    assert.match(source, new RegExp(`(?:state === |case )["']${state}["']`));
  }
  assert.match(source, /Получить 7 дней/);
  assert.match(source, /Новые подключения временно приостановлены/);
  assert.match(source, /Обновить статус/);
  assert.match(source, /href="#profiles"/);
  assert.match(source, /href="#plans"/);
  assert.doesNotMatch(source, /config_uri|external_uuid|worker_id/);

  const capacityBranch = source.slice(
    source.indexOf('case "capacity_paused"'),
    source.indexOf('case "preparing"'),
  );
  assert.doesNotMatch(capacityBranch, /onActivate/);
  const availableBranch = source.slice(
    source.indexOf('case "available"'),
    source.indexOf('case "capacity_paused"'),
  );
  assert.match(availableBranch, /onActivate/);
});

test("trial polling schedules exactly thirty non-overlapping two-second attempts", () => {
  const timers = [];
  const delays = [];
  const schedule = (callback, delay) => {
    delays.push(delay);
    timers.push(callback);
    return timers.length;
  };
  let completed = 0;

  const scheduleNext = () => nextTrialPoll(completed, () => {
    completed = trialPolling.updateTrialPollCount(completed, "automatic");
    scheduleNext();
  }, schedule);

  scheduleNext();
  while (timers.length > 0) {
    assert.equal(timers.length, 1);
    timers.shift()();
  }

  assert.equal(completed, 30);
  assert.equal(delays.length, 30);
  assert.deepEqual(new Set(delays), new Set([2_000]));
  assert.equal(nextTrialPoll(completed, () => assert.fail("poll limit exceeded"), schedule), null);
  assert.equal(timers.length, 0);
});

test("manual refresh preserves an exhausted auto-poll budget", async () => {
  assert.equal(typeof trialPolling.updateTrialPollCount, "function");
  const updateTrialPollCount = trialPolling.updateTrialPollCount;
  const timers = [];
  const schedule = (callback) => {
    timers.push(callback);
    return timers.length;
  };
  let completedPolls = 30;
  let trialState = "preparing";
  let requests = 0;

  const scheduleIfPreparing = () => trialState === "preparing"
    ? nextTrialPoll(completedPolls, () => {}, schedule)
    : null;
  const manualRefresh = async (responseState) => {
    completedPolls = updateTrialPollCount(completedPolls, "manual");
    requests += 1;
    trialState = await Promise.resolve(responseState);
  };

  await manualRefresh("preparing");
  assert.equal(requests, 1);
  assert.equal(completedPolls, 30);
  assert.equal(scheduleIfPreparing(), null);
  assert.equal(timers.length, 0);

  await manualRefresh("active");
  assert.equal(requests, 2);
  assert.equal(trialState, "active");
  assert.equal(scheduleIfPreparing(), null);

  completedPolls = updateTrialPollCount(completedPolls, "activation");
  trialState = "preparing";
  assert.equal(completedPolls, 0);
  assert.notEqual(scheduleIfPreparing(), null);

  completedPolls = 30;
  assert.equal(updateTrialPollCount(completedPolls, "session"), 0);
});

test("portal loads, activates and bounds trial polling through the session generation", async () => {
  const source = await readFile(portalUrl, "utf8");

  assert.match(source, /portalApi\.trial\(\)/);
  assert.match(source, /portalApi\.subscriptions\(\)/);
  assert.match(source, /portalApi\.profiles\(\)/);
  assert.match(source, /portalApi\.activateTrial\(me\.csrf_token\)/);
  assert.match(source, /setTrial\(activatedTrial\)/);
  assert.match(source, /nextTrialPoll\(/);
  assert.match(source, /sessionGeneration\.isCurrent\(generation\)/);
  assert.match(source, /clearTimeout\(timer\)/);
  assert.match(source, /trial\?\.state !== "preparing"/);
  assert.match(source, /updateTrialPollCount\(current, "automatic"\)/);
  const refreshSource = source.slice(
    source.indexOf("async function refreshTrial"),
    source.indexOf("useEffect(() =>", source.indexOf("async function refreshTrial")),
  );
  assert.doesNotMatch(refreshSource, /setTrialPollCount\(0\)/);
  assert.match(refreshSource, /updateTrialPollCount\(current, "manual"\)/);
  assert.match(source, /updateTrialPollCount\(current, "activation"\)/);
  assert.match(source, /updateTrialPollCount\(current, "session"\)/);
});

test("trial styling reuses portal tokens and makes actions full-width on mobile", async () => {
  const source = await readFile(cssUrl, "utf8");

  assert.match(source, /\.trial-card\s*\{/);
  assert.match(source, /\.trial-card__meta\s*\{/);
  assert.match(source, /\.trial-card__actions\s*\{/);
  assert.match(source, /@media \(max-width: 640px\)[\s\S]*\.trial-card__actions[\s\S]*width:\s*100%/);
});
