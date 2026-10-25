import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";

async function sources() {
  return (await Promise.all([
    readFile(new URL("../src/App.tsx", import.meta.url), "utf8"),
    readFile(new URL("../src/VpnCustomerWorkspacePanel.tsx", import.meta.url), "utf8"),
    readFile(new URL("../src/styles.css", import.meta.url), "utf8"),
  ])).map((source) => source.replace(/\r\n/g, "\n"));
}

test("App loads only safe invitation list views and never owns a raw link", async () => {
  const [appSource] = await sources();

  assert.match(
    appSource,
    /const \[vpnFriendInvitations, setVpnFriendInvitations\] = useState<VpnFriendInvitation\[\]>\(\[\]\)/,
  );
  assert.match(appSource, /api\.getVpnFriendInvitations\(\)/);
  assert.match(appSource, /setVpnFriendInvitations\(vpnFriendInvitationsData\)/);
  assert.match(appSource, /friendInvitations=\{vpnFriendInvitations\}/);
  assert.doesNotMatch(appSource, /invite_link/);
});

test("workspace renders unlimited Russian invitations with guarded actions", async () => {
  const [, panelSource] = await sources();

  assert.match(panelSource, /<h3>Приглашения<\/h3>/);
  assert.doesNotMatch(panelSource, /usedInvitationCount|invitationsReady|Тестовые приглашения|\/10/);
  assert.match(panelSource, /Создать приглашение/);
  assert.match(panelSource, /7 дней[\s\S]*?7 дней/);
  assert.match(panelSource, /Загрузить ещё/);
  for (const label of [
    "Не использовано",
    "Готовится",
    "Активно",
    "Ошибка",
    "Требует проверки",
    "Истекло",
    "Отключено",
  ]) {
    assert.ok(panelSource.includes(label), `missing Russian state label: ${label}`);
  }
  assert.match(panelSource, /invitation\.can_rotate \? \(/);
  assert.match(panelSource, /invitation\.can_retry \? \(/);
  assert.match(panelSource, /invitation\.can_disable \? \(/);
  assert.doesNotMatch(
    panelSource,
    /invite_state === "needs_verification"[\s\S]{0,160}retryVpnFriendInvitation/,
  );
});

test("one-time links remain component-local and are published before reload", async () => {
  const [appSource, panelSource] = await sources();

  assert.match(
    panelSource,
    /const \[invitationLinks, setInvitationLinks\] = useState<Record<number, string>>\(\{\}\)/,
  );
  assert.match(panelSource, /const inviteLink = invitationLinks\[invitation\.slot\]/);
  assert.match(panelSource, /navigator\.clipboard\.writeText\(inviteLink\)/);
  assert.match(panelSource, /inviteLink \? \(/);
  assert.match(
    panelSource,
    /setInvitationLinks\([\s\S]*?issued\.invite_link[\s\S]*?await reload\(\)/,
  );
  assert.match(panelSource, /api\.issueVpnFriendInvitation\(\)/);
  assert.match(panelSource, /api\.rotateVpnFriendInvitation\(invitation\.slot\)/);
  const rotateAction = panelSource.indexOf("async function rotateFriendInvitation");
  const forgetOldLink = panelSource.indexOf(
    "forgetInvitationLink(invitation.slot)",
    rotateAction,
  );
  const rotateMutation = panelSource.indexOf(
    "api.rotateVpnFriendInvitation(invitation.slot)",
    rotateAction,
  );
  assert.ok(forgetOldLink > rotateAction);
  assert.ok(rotateMutation > forgetOldLink, "the old link must disappear before rotation");
  assert.doesNotMatch(appSource, /invite_link/);
  assert.doesNotMatch(
    panelSource,
    /localStorage|sessionStorage|URLSearchParams|window\.location|history\.|console\./,
  );
});

test("one-time links disappear after the server reports a non-usable slot", async () => {
  const [, panelSource] = await sources();

  assert.match(
    panelSource,
    /useEffect\(\(\) => \{[\s\S]*?forgetUnusableInvitationLinks\(friendInvitations\)[\s\S]*?\}, \[friendInvitations\]\)/,
  );
  assert.match(panelSource, /rows\.filter\(\(invitation\) => !\(invitation\.invite_state === "unused" && invitation\.can_rotate\)\)/);
  assert.match(panelSource, /Object\.entries\(current\)\.filter\(\(\[slot\]\) => !unusableSlots\.has\(Number\(slot\)\)\)/);
  assert.match(panelSource, /forgetUnusableInvitationLinks\(page\)/);
});

test("disable requires confirmation before the mutation", async () => {
  const [, panelSource] = await sources();
  const action = panelSource.indexOf("async function disableFriendInvitation");
  const confirmation = panelSource.indexOf("window.confirm", action);
  const mutation = panelSource.indexOf("api.disableVpnFriendInvitation", action);

  assert.notEqual(action, -1);
  assert.ok(confirmation > action, "confirmation must be inside the disable action");
  assert.ok(mutation > confirmation, "disable API call must happen only after confirmation");
  assert.match(panelSource, /потеряет доступ к личному кабинету и VPN/);
});

test("saved retry and disable are not reported as mutation failures when reload fails", async () => {
  const [, panelSource] = await sources();
  const retryStart = panelSource.indexOf("async function retryFriendInvitation");
  const disableStart = panelSource.indexOf("async function disableFriendInvitation");
  const copyStart = panelSource.indexOf("async function copyInvitationLink");
  const retrySource = panelSource.slice(retryStart, disableStart);
  const disableSource = panelSource.slice(disableStart, copyStart);

  assert.match(
    panelSource,
    /async function reloadAfterSavedInvitation[\s\S]*?await reload\(\)[\s\S]*?Изменение сохранено, но список не обновился/,
  );
  assert.ok(
    retrySource.indexOf("api.retryVpnFriendInvitation") <
      retrySource.indexOf('notify("success"'),
  );
  assert.ok(
    retrySource.indexOf('notify("success"') <
      retrySource.indexOf("reloadAfterSavedInvitation()"),
  );
  assert.match(retrySource, /Не удалось повторить выдачу/);
  assert.ok(
    disableSource.indexOf("api.disableVpnFriendInvitation") <
      disableSource.indexOf("forgetInvitationLink(invitation.slot)"),
  );
  assert.ok(
    disableSource.indexOf("forgetInvitationLink(invitation.slot)") <
      disableSource.indexOf('notify("success"'),
  );
  assert.ok(
    disableSource.indexOf('notify("success"') <
      disableSource.indexOf("reloadAfterSavedInvitation()"),
  );
  assert.match(disableSource, /Не удалось отключить участника/);
});

test("invitation rows and raw links stay compact on narrow screens", async () => {
  const [, , styleSource] = await sources();

  assert.match(
    styleSource,
    /\.vpn-friend-invitation-row \{[^}]*grid-template-columns: auto minmax\(0, 1fr\) auto;/s,
  );
  assert.match(
    styleSource,
    /\.vpn-friend-invitation-link \{[^}]*overflow-wrap: anywhere;/s,
  );
  assert.match(
    styleSource,
    /@media \(max-width: 720px\)[\s\S]*\.vpn-friend-invitation-row \{[^}]*grid-template-columns: minmax\(0, 1fr\);/s,
  );
});
