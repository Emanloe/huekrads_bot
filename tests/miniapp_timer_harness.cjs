"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

class Node {
  constructor(tag = "div") {
    this.tag = tag;
    this.className = "";
    this.textContent = "";
    this.children = [];
    this.disabled = false;
    this.listeners = {};
    this.parent = null;
    this.classList = {
      toggle() {},
      add: name => { this.className += ` ${name}`; },
    };
  }
  append(...nodes) { for (const node of nodes) { node.parent = this; this.children.push(node); } }
  replaceChildren(...nodes) {
    for (const child of this.children) child.parent = null;
    this.children = [];
    this.append(...nodes);
  }
  insertBefore(node, reference) {
    const index = this.children.indexOf(reference);
    assert.notEqual(index, -1);
    node.parent = this;
    this.children.splice(index, 0, node);
  }
  remove() {
    if (!this.parent) return;
    const index = this.parent.children.indexOf(this);
    if (index !== -1) this.parent.children.splice(index, 1);
    this.parent = null;
  }
  querySelector(selector) {
    const className = selector.startsWith(".") ? selector.slice(1) : null;
    for (const child of this.children) {
      if (className && child.className.split(" ").includes(className)) return child;
      const descendant = child.querySelector(selector);
      if (descendant) return descendant;
    }
    return null;
  }
  addEventListener(name, callback) { this.listeners[name] = callback; }
  setAttribute() {}
  removeAttribute() {}
  get childElementCount() { return this.children.length; }
  querySelectorAll(selector) {
    const found = [];
    const visit = (node, inZone = false, inBoss = false) => {
      const zone = inZone || node.className.split(" ").includes("zone-row");
      const boss = inBoss || node.className.split(" ").includes("boss-action");
      if (selector === ".zone-row button" && zone && node.tag === "button" ||
          selector === ".boss-action button" && boss && node.tag === "button") found.push(node);
      for (const child of node.children) visit(child, zone, boss);
    };
    visit(this);
    return found;
  }
}

const nodes = new Map();
for (const view of ["home", "opponents", "duel", "hall", "boss"]) {
  const screen = new Node("section");
  const content = new Node();
  content.className = "panel-body";
  screen.append(content);
  if (view === "home") screen.dataset = { gnomeSrc: "/media/gnome?v=test-version" };
  nodes.set(`screen-${view}`, screen);
  nodes.set(`${view}-content`, content);
}
const intervals = [];
const timeouts = [];
let now = 0;
let wallOffset = 0;
let fetchCount = 0;
let moveCount = 0;
let moveMode = "accepted";
let getMode = "ok";
let serverDuel = null;
let profileFetchCount = 0;
let hallFetchCount = 0;
let bossFetchCount = 0;
let bossActionCount = 0;
let bossActionBodies = [];
let bossPostMode = "accepted";
let bossPostRelease = null;
let bossModel = { battle: null, registration: {
  open: true, participants_count: 0, viewer_registered: false,
}, viewer: { in_battle: false } };
const document = {
  hidden: false,
  listeners: {},
  createElement: tag => new Node(tag),
  getElementById: id => nodes.get(id),
  querySelectorAll: selector => selector === ".zone-row button" ?
    nodes.get("duel-content").querySelectorAll(selector) :
    selector === ".boss-action button" ?
      nodes.get("boss-content").querySelectorAll(selector) : [],
  addEventListener(name, callback) { this.listeners[name] = callback; },
};
const window = {
  setInterval(callback, delay) { intervals.push({ callback, delay }); return intervals.length; },
  setTimeout(callback, delay) { timeouts.push({ callback, delay }); return timeouts.length; },
  clearTimeout() {},
};
const epoch = 1_700_000_000_000;
const fetch = async (url, options) => {
  if (url === "/api/v1/boss") {
    assert.equal(options.method, "GET");
    bossFetchCount++;
    return {
      ok: true,
      headers: { get: name => name === "content-type" ? "application/json" :
        name === "X-Boss-Server-Time-Ms" ? String(epoch + now) : null },
      json: async () => bossModel,
    };
  }
  if (url === "/api/v1/boss/join" || url === "/api/v1/boss/action") {
    assert.equal(options.method, "POST");
    bossActionCount++;
    const body = JSON.parse(options.body);
    bossActionBodies.push({ url, body });
    if (bossPostMode === "hold") {
      await new Promise(resolve => { bossPostRelease = resolve; });
    }
    if (bossPostMode === "stale") return {
      ok: false, status: 409,
      headers: { get: name => name === "content-type" ? "application/json" : null },
      json: async () => ({ detail: { code: "stale_phase", message: "Фаза изменилась." } }),
    };
    if (url.endsWith("/join")) {
      bossModel.viewer = { in_battle: true, alive: true, choice_submitted: null };
      bossModel.available_actions = [];
    } else {
      bossModel.viewer.choice_submitted = true;
      bossModel.viewer.selected_action = body.action_id;
    }
    return {
      ok: true,
      headers: { get: name => name === "content-type" ? "application/json" : null },
      json: async () => ({ accepted: true }),
    };
  }
  if (url === "/api/v1/duel/hall-of-fame") {
    assert.equal(options.method, "GET");
    hallFetchCount++;
    return {
      ok: true,
      headers: { get: name => name === "content-type" ? "application/json" : null },
      json: async () => ({ sort_by: "wins", players: [{
        rank: 1, user_id: 202, title: "<script>alert(1)</script>",
        points: 30, wins: 10, losses: 2,
        gnome_image_url: "/media/gnome/gnome_07?v=hall-version",
      }] }),
    };
  }
  if (url === "/api/v1/players/202") {
    assert.equal(options.method, "GET");
    profileFetchCount++;
    return {
      ok: true,
      headers: { get: name => name === "content-type" ? "application/json" : null },
      json: async () => ({
        display_name: "<script>alert(1)</script>", dwarf_name: "<img src=x>", username: "target",
        gnome_image_url: "/media/gnome/gnome_02?v=target-version",
        points: 0, max_points: 100, wins: 100, losses: 2, daily_wins: 0,
        ineligibility: "no_dick", boss_wins: 3, dick_status: { text: "Без хуя" },
        titles: { wins: { text: "<b>title</b>", count: 100 },
          losses: { text: null, count: 2 }, stolen_dicks: { text: null, count: 0 } },
        inventory: [{ name: "<unsafe item>", count: 1 }], pet: null,
      }),
    };
  }
  if (url === "/api/v1/duel/move") {
    assert.equal(options.method, "POST");
    moveCount++;
    if (moveMode === "uncertain") throw new Error("response lost after commit");
    return {
      ok: true,
      headers: { get: name => name === "content-type" ? "application/json" : null },
      json: async () => ({ accepted: true }),
    };
  }
  assert.equal(url, "/api/v1/duel/active");
  fetchCount++;
  if (getMode === "network") throw new Error("network unavailable");
  if (getMode === "expired") return {
    ok: false, status: 401,
    headers: { get: name => name === "content-type" ? "application/json" : null },
    json: async () => ({ detail: { code: "session_expired" } }),
  };
  return {
    ok: true,
    headers: { get: name => name === "content-type" ? "application/json" :
      name === "X-Duel-Server-Time-Ms" ? String(epoch + now) : null },
    json: async () => ({ duel: serverDuel, recent_finished: null }),
  };
};
const source = fs.readFileSync(path.join(__dirname, "..", "miniapp_static", "app.js"), "utf8");
const boot = /\s+bootstrap\(\);\s*\}\)\(\);\s*$/;
assert.ok(boot.test(source));
const instrumented = source.replace(boot, `
  globalThis.appTest = {
    renderHome, renderProfile, renderDuel, renderOpponents, renderHall, renderBoss,
    updateCountdown, updateBossCountdown, loadView, submitMove, submitBossAction,
    setContext(token, view) { sessionToken = token; currentView = view; },
    get countdownTurn() { return countdownTurn; },
  };
})();`);
const sandbox = {
  document, window, fetch,
  performance: { now: () => now },
  Date: { now: () => epoch + now + wallOffset },
  AbortController,
  globalThis: null,
};
sandbox.globalThis = sandbox;
vm.runInNewContext(instrumented, sandbox, { filename: "app.js" });
const app = sandbox.appTest;
app.setContext("test-session", "duel");
assert.deepEqual(intervals.map(item => item.delay), [1000, 250, 1000]);

const round = { round: 1, outcome: "hit", attacker: { display_name: "A" },
  defender: { display_name: "B" }, attack_zone: "head", defense_zone: "body" };
function duel(overrides = {}) {
  return {
    id: 1, turn_id: 2, round: 2, status: "active", phase: "attack",
    role: "attacker", can_act: true, own_attack_accepted: false,
    deadline_at: epoch + 9600,
    attacker: { display_name: "A" }, defender: { display_name: "B" },
    rounds: [round], ...overrides,
  };
}
function render(value, clock = { serverNow: epoch + now, observedAt: now }) {
  app.renderDuel({ duel: value, recent_finished: null, _clock: clock });
}
function findClass(node, className) {
  return node.children.find(child => child.className.split(" ").includes(className));
}
const ownProfile = {
  display_name: "<script>alert(1)</script>", dwarf_name: "<img src=x>",
  gnome_variant: "gnome_07", gnome_image_url: "/media/gnome/gnome_07?v=test-version",
  points: 30, max_points: 100, wins: 140, losses: 123, daily_wins: 2,
  ineligibility: "no_dick", boss_wins: 3, dick_status: { text: "Без хуя" },
  titles: {}, inventory: [], pet: null,
};
app.renderHome(ownProfile);
const homeContent = nodes.get("home-content").children[0];
const homeProfile = findClass(homeContent, "home-profile");
assert.ok(homeProfile);
assert.equal(homeProfile.children[0].tag, "img");
assert.equal(homeProfile.children[0].src, ownProfile.gnome_image_url);
assert.equal(homeProfile.children[0].width, 200);
assert.equal(homeProfile.children[0].height, 200);
assert.equal(homeProfile.children[1].children.length, 6);
assert.equal(homeProfile.children[1].children[0].children[1].textContent, "<script>alert(1)</script>");
assert.deepEqual(homeContent.children.filter(child => child.tag === "h3").map(child => child.textContent),
  ["Хуяние", "Статус", "Инвентарь"]);
app.renderProfile(ownProfile, nodes.get("opponents-content"), true);
const inspectedProfile = nodes.get("opponents-content").children[0];
const inspectedHero = findClass(inspectedProfile, "home-profile");
assert.equal(inspectedHero.children[0].src, ownProfile.gnome_image_url);
assert.equal(inspectedHero.children[1].children.length, 7);
assert.equal(findClass(inspectedProfile, "inspect-back").textContent, "← К соперникам");
assert.equal(findClass(inspectedProfile, "hint").textContent, "Профиль игрока · только просмотр");
assert.deepEqual(inspectedProfile.children.filter(child => child.tag === "h3").map(child => child.textContent),
  ["Хуяние", "Статус", "Инвентарь"]);
function countdownText() { return app.countdownTurn?.node.textContent; }
function actionPanel() {
  return findClass(nodes.get("duel-content").children[0], "action-box");
}
function actionButtons() {
  return nodes.get("duel-content").querySelectorAll(".zone-row button");
}

// The server's 9.6 seconds round up to 10; local ticks need no HTTP response.
render(duel());
assert.equal(countdownText(), "10 сек.");
assert.equal(fetchCount, 0);
for (const [time, expected] of [[650, "9 сек."], [1650, "8 сек."], [2650, "7 сек."]]) {
  now = time;
  app.updateCountdown();
  assert.equal(countdownText(), expected);
}
assert.equal(fetchCount, 0);
const firstNode = app.countdownTurn.node;
const root = nodes.get("duel-content").children[0];
assert.ok(root.children.indexOf(findClass(root, "action-box")) <
  root.children.indexOf(findClass(root, "round-history")));
assert.equal(nodes.get("duel-content").querySelectorAll(".zone-row button").length, 3);
assert.equal(actionPanel().children[0].textContent, "Атака");
assert.ok(actionButtons().every(button => !button.disabled));

// The visual clock follows the server sample even if the device clock is wrong.
wallOffset = 60_000;
render(duel(), { serverNow: epoch + now, observedAt: now });
assert.equal(countdownText(), "7 сек.");
wallOffset = 0;

// A later turn replaces the old timer, including when rounds accumulate.
render(duel({ turn_id: 3, rounds: [round, { ...round, round: 2 }], deadline_at: epoch + 12500 }));
assert.equal(intervals.length, 3);
assert.equal(firstNode.textContent, "7 сек.");
now = 3650;
app.updateCountdown();
assert.equal(firstNode.textContent, "7 сек.");
assert.equal(countdownText(), "9 сек.");
const secondRoot = nodes.get("duel-content").children[0];
assert.ok(secondRoot.children.indexOf(findClass(secondRoot, "action-box")) <
  secondRoot.children.indexOf(findClass(secondRoot, "round-history")));

// The panel describes this user's action, while the state grid retains server phase.
render(duel({ turn_id: 4, phase: "block", role: "defender" }));
const blockRoot = nodes.get("duel-content").children[0];
assert.ok(blockRoot.children.indexOf(findClass(blockRoot, "action-box")) <
  blockRoot.children.indexOf(findClass(blockRoot, "round-history")));
assert.equal(actionPanel().children[0].textContent, "Блок");
assert.ok(actionButtons().every(button => !button.disabled));

// The server distinguishes a submitted attack from a timeout-selected zone.
render(duel({ turn_id: 5, phase: "block", role: "attacker", can_act: false,
  attack_zone: "head", own_attack_accepted: true }));
assert.equal(actionPanel().children[0].textContent, "Атака");
assert.equal(actionPanel().children[1].textContent, "Атака принята. Ожидаем соперника…");
assert.equal(actionButtons().length, 0);
assert.ok(!actionPanel().children[1].textContent.includes("Блок принят"));
render(duel({ turn_id: 5, phase: "block", role: "attacker", can_act: false,
  attack_zone: null }));
assert.equal(actionPanel().children[0].textContent, "Ожидание");
assert.equal(actionPanel().children[1].textContent, "Ожидаем сервер или другого участника.");
assert.equal(actionButtons().length, 0);

// Waiting for the attack must not claim the defender has submitted a block.
render(duel({ turn_id: 6, phase: "attack", role: "defender", can_act: false }));
assert.equal(actionPanel().children[0].textContent, "Ожидание");
assert.equal(actionPanel().children[1].textContent, "Соперник выбирает зону атаки…");
assert.equal(actionButtons().length, 0);

// A submitted block resolves the round immediately; there is no active
// "block accepted" state to claim while the next prompt is publishing.
render(duel({ turn_id: 7, status: "publishing", phase: "attack",
  role: "attacker", can_act: false, deadline_at: null }));
assert.equal(actionPanel(), undefined);
render(duel({ turn_id: 8, status: "publishing", phase: "block",
  role: "attacker", can_act: false, attack_zone: "body",
  own_attack_accepted: true, deadline_at: null }));
assert.equal(actionPanel().children[0].textContent, "Атака");
assert.equal(actionPanel().children[1].textContent,
  "Атака принята. Ожидаем соперника…");
assert.equal(actionButtons().length, 0);
render(duel({ turn_id: 9, phase: "block", role: "spectator", can_act: false }));
assert.equal(actionPanel(), undefined);

render(duel({ turn_id: 6, rounds: [] }));
assert.equal(findClass(nodes.get("duel-content").children[0], "round-history"), undefined);

// At zero, only display/control state changes; a GET is scheduled once.
render(duel({ turn_id: 7, deadline_at: epoch + now + 100 }));
now += 150;
app.updateCountdown();
assert.equal(countdownText(), "Время вышло · ждём сервер");
assert.equal(actionButtons().length, 3);
assert.ok(actionButtons().every(button => button.disabled));
const scheduled = timeouts.filter(item => item.delay === 0).length;
app.updateCountdown();
assert.equal(timeouts.filter(item => item.delay === 0).length, scheduled);
assert.equal(fetchCount, 0);

// Finished/no active state has no countdown or action box.
render(null);
assert.equal(app.countdownTurn, null);
app.updateCountdown();
assert.equal(nodes.get("duel-content").querySelectorAll(".zone-row button").length, 0);
assert.equal(intervals.length, 3);
app.renderDuel({ duel: null, recent_finished: {
  id: 1, player1: { display_name: "A" }, player2: { display_name: "B" },
  winner: { display_name: "A" }, loser: { display_name: "B" },
  points: { winner_before: 1, winner_after: 2, loser_before: 2, loser_after: 1 },
  rounds: [],
} });
assert.equal(app.countdownTurn, null);
assert.equal(nodes.get("duel-content").querySelectorAll(".zone-row button").length, 0);

async function testPolling() {
  now = 20_000;
  serverDuel = duel({ deadline_at: epoch + now + 10_000 });
  await app.loadView("duel", true);
  assert.equal(fetchCount, 1);
  now += 1000;
  intervals[0].callback();
  await Promise.resolve();
  await Promise.resolve();
  assert.equal(fetchCount, 2);

  serverDuel = null;
  await app.loadView("duel", true);
  const idleCount = fetchCount;
  now += 1000;
  intervals[0].callback();
  assert.equal(fetchCount, idleCount);
  document.hidden = true;
  now += 10_000;
  intervals[0].callback();
  assert.equal(fetchCount, idleCount);
  document.hidden = false;
  document.listeners.visibilitychange();
  await Promise.resolve();
  assert.equal(fetchCount, idleCount + 1);

  // The next authoritative poll promotes waiting defender to actionable block.
  now = 30_000;
  serverDuel = duel({ turn_id: 10, phase: "attack", role: "defender",
    can_act: false, deadline_at: epoch + now + 10_000 });
  await app.loadView("duel", true);
  assert.equal(actionPanel().children[0].textContent, "Ожидание");
  assert.equal(actionButtons().length, 0);
  serverDuel = duel({ turn_id: 11, phase: "block", role: "defender",
    can_act: true, deadline_at: epoch + now + 10_000 });
  await app.loadView("duel", true);
  assert.equal(actionPanel().children[0].textContent, "Блок");
  assert.equal(actionButtons().length, 3);
  assert.ok(actionButtons().every(button => !button.disabled));
  serverDuel = duel({ turn_id: 12, status: "publishing", phase: "attack",
    role: "attacker", can_act: false, deadline_at: null });
  await app.loadView("duel", true);
  assert.equal(actionPanel(), undefined);

  // Accepted and uncertain writes each trigger a fresh GET, never a blind retry.
  await app.loadView("duel", true);
  now = 35_000;
  render(duel({ turn_id: 20, deadline_at: epoch + now + 10_000 }));
  serverDuel = duel({ turn_id: 21, phase: "block", role: "attacker", can_act: false,
    deadline_at: epoch + now + 10_000 });
  const beforeAccepted = fetchCount;
  await app.submitMove("head");
  assert.equal(moveCount, 1);
  assert.equal(fetchCount, beforeAccepted + 1);
  assert.equal(app.countdownTurn.turnId, 21);

  render(duel({ turn_id: 22, deadline_at: epoch + now + 10_000 }));
  serverDuel = duel({ turn_id: 23, phase: "block", role: "attacker", can_act: false,
    deadline_at: epoch + now + 10_000 });
  moveMode = "uncertain";
  const beforeUncertain = fetchCount;
  await app.submitMove("body");
  assert.equal(moveCount, 2);
  assert.equal(fetchCount, beforeUncertain + 1);
  assert.equal(app.countdownTurn.turnId, 23);

  // Network errors stay in the relevant tab and clear after a successful poll.
  getMode = "network";
  assert.equal(await app.loadView("duel", true), false);
  const duelScreen = nodes.get("screen-duel");
  assert.ok(duelScreen.querySelector(".view-message").textContent.includes("Нет связи"));
  getMode = "ok";
  assert.equal(await app.loadView("duel", true), true);
  assert.equal(duelScreen.querySelector(".view-message"), null);

  // Session expiry remains visible on the active tab and asks for /duel_app.
  getMode = "expired";
  assert.equal(await app.loadView("duel", true), false);
  assert.ok(nodes.get("duel-content").children[0].textContent.includes("/duel_app"));
  assert.equal(duelScreen.querySelector(".view-message"), null);
  const afterExpiry = fetchCount;
  now += 1000;
  intervals[0].callback();
  assert.equal(fetchCount, afterExpiry);

  // A dickless viewer still has a card and can inspect a dickless target.
  app.setContext("test-session", "opponents");
  app.renderOpponents({
    ineligibility: "no_dick", opponents: [{
      user_id: 202, username: "<unsafe>", title: "Other", points: 0,
      wins: 100, losses: 2, duel_ineligibility: "no_dick",
      titles: { wins: { text: "Winner", count: 100 } },
    }],
  });
  const opponentBody = nodes.get("opponents-content");
  const list = findClass(opponentBody.children[0], "opponent-list");
  assert.equal(list.children.length, 1);
  const actions = findClass(list.children[0], "opponent-actions");
  assert.equal(actions.children[0].textContent, "Осмотреть");
  assert.equal(actions.children[0].disabled, false);
  assert.equal(actions.children[1].disabled, true);
  assert.equal(actions.children[2].textContent, "Сегодня без хуя");
  await actions.children[0].listeners.click();
  assert.equal(profileFetchCount, 1);
  const profile = opponentBody.children[0];
  assert.equal(findClass(profile, "inspect-back").textContent, "← К соперникам");
  const targetHero = findClass(profile, "home-profile");
  assert.equal(targetHero.children[0].src, "/media/gnome/gnome_02?v=target-version");
  assert.equal(targetHero.children[1].children[0].children[1].textContent,
    "<script>alert(1)</script>");
  assert.equal(targetHero.children[1].children[6].children[1].textContent, "@target");
  findClass(profile, "inspect-back").listeners.click();
  assert.equal(findClass(opponentBody.children[0], "opponent-list").children.length, 1);

  // The hall uses the target avatar and the same read-only inspect screen.
  app.setContext("test-session", "hall");
  assert.equal(await app.loadView("hall"), true);
  assert.equal(hallFetchCount, 1);
  const hallBody = nodes.get("hall-content");
  const hallList = findClass(hallBody.children[0], "hall-list");
  const hallRow = hallList.children[0];
  assert.equal(hallRow.children[0].textContent, "1.");
  assert.equal(hallRow.children[1].src, "/media/gnome/gnome_07?v=hall-version");
  assert.equal(hallRow.children[1].width, 36);
  assert.equal(hallRow.children[1].height, 36);
  assert.equal(hallRow.children[2].children[0].textContent, "<script>alert(1)</script>");
  assert.equal(hallRow.children[3].textContent, "Осмотреть");
  await hallRow.children[3].listeners.click();
  assert.equal(profileFetchCount, 2);
  const hallProfile = hallBody.children[0];
  assert.equal(findClass(hallProfile, "inspect-back").textContent, "← К залу славы");
  assert.equal(findClass(hallProfile, "home-profile").children[0].src,
    "/media/gnome/gnome_02?v=target-version");
  app.renderOpponents({ ineligibility: null, opponents: [] });
  assert.ok(nodes.get("opponents-content").children[0].children[0].textContent.includes("нет доступных"));
  findClass(hallProfile, "inspect-back").listeners.click();
  assert.equal(findClass(hallBody.children[0], "hall-list").children.length, 1);

  app.renderHall({ sort_by: "wins", players: [] });
  assert.ok(hallBody.children[0].children[0].textContent.includes("пока пуста"));

  // Boss reads use their own cadence, never the active duel timer.
  app.setContext("test-session", "boss");
  assert.equal(await app.loadView("boss"), true);
  assert.equal(bossFetchCount, 1);
  const bossBody = nodes.get("boss-content");
  assert.ok(bossBody.children[0].children[0].textContent.includes("Сейчас битвы"));
  assert.ok(bossBody.children[0].children[2].textContent.includes("/boss_reg"));
  intervals[2].callback();
  assert.equal(bossFetchCount, 1);
  now += 8000;
  intervals[2].callback();
  await Promise.resolve();
  assert.equal(bossFetchCount, 2);

  bossModel = {
    battle: {
      battle_id: "battle_join_001", deadline_at: epoch + now + 30000,
      boss: { id: "deep_snouted_baron", name: "Босс", emoji: "⚔️",
        description: "Набор" },
      phase: "join", round: 0, hits: 0, required_hits: 5,
      participants_count: 0, alive_count: 0, participants: [],
    },
    registration: null, available_actions: [{ id: "join", label: "Вступить в бой" }],
    viewer: { in_battle: false, alive: null, choice_submitted: null },
  };
  await app.loadView("boss", true);
  assert.equal(findClass(bossBody.children[0], "boss-action").children[0].textContent,
    "Вступить в бой");
  assert.equal(await app.submitBossAction("join"), undefined);
  assert.equal(bossActionCount, 1);
  assert.equal(bossActionBodies[0].url, "/api/v1/boss/join");
  assert.equal(bossActionBodies[0].body.battle_id, "battle_join_001");
  assert.equal(bossActionBodies[0].body.round, 0);
  assert.equal(findClass(bossBody.children[0], "boss-action"), undefined);

  bossModel = {
    battle: {
      battle_id: "battle_attack_001", deadline_at: epoch + now + 5000,
      boss: { id: "chizyanovsky_skier", name: "<script>Босс</script>",
        emoji: "🎿", description: "<img src=x>" },
      phase: "attack", round: 2, hits: 3, required_hits: 5,
      participants_count: 2, alive_count: 1,
      participants: [
        { user_id: 101, title: "<b>Гном</b>", alive: true, hits: 2,
          choice_submitted: false },
        { user_id: 202, title: "Друг", alive: false, hits: 1,
          choice_submitted: null },
      ],
    },
    registration: null,
    available_actions: [
      { id: "head", label: "<img src=x>" },
      { id: "body", label: "Торс" },
      { id: "dick", label: "Хуй" },
    ],
    viewer: { in_battle: true, alive: true, hits: 2,
      choice_submitted: false, can_act_in_telegram: true },
  };
  assert.equal(await app.loadView("boss", true), true);
  const bossContent = bossBody.children[0];
  const banner = findClass(bossContent, "boss-banner");
  assert.equal(banner.children[1].children[0].textContent, "<script>Босс</script>");
  assert.equal(banner.children[1].children[1].textContent, "<img src=x>");
  const progress = findClass(bossContent, "boss-progress");
  assert.equal(progress.value, 3);
  assert.equal(progress.max, 5);
  const team = findClass(bossContent, "boss-team");
  assert.equal(team.children[0].children[0].textContent, "<b>Гном</b>");
  assert.equal(team.children[1].children[1].textContent, "Выбыл");
  assert.equal(bossContent.children.filter(node => node.tag === "button").length, 0);
  const action = findClass(bossContent, "boss-action");
  assert.equal(action.children.length, 3);
  assert.equal(action.children[0].textContent, "<img src=x>");
  bossPostMode = "hold";
  const pendingAttack = app.submitBossAction("head");
  assert.ok(document.querySelectorAll(".boss-action button").every(button => button.disabled));
  bossPostRelease();
  assert.equal(await pendingAttack, undefined);
  bossPostMode = "accepted";
  assert.equal(bossActionCount, 2);
  assert.deepEqual(bossActionBodies[1].body, {
    battle_id: "battle_attack_001", round: 2, phase: "attack", action_id: "head",
  });
  assert.ok(findClass(bossBody.children[0], "boss-action").children[0].className
    .includes("is-selected"));
  bossPostMode = "stale";
  const beforeStale = bossFetchCount;
  await app.submitBossAction("body");
  assert.equal(bossFetchCount, beforeStale + 1);
  bossPostMode = "accepted";
  const deadline = bossBody.querySelector(".data-grid").children.at(-1).children.at(-1);
  assert.equal(deadline.textContent, "5 с");
  now += 5000;
  app.updateBossCountdown();
  assert.equal(deadline.textContent, "0 с");
  assert.ok(document.querySelectorAll(".boss-action button").every(button => button.disabled));
  bossModel.battle.phase = "block";
  bossModel.battle.deadline_at = epoch + now + 10000;
  bossModel.available_actions = [{ id: "head", label: "🛡 Голова" }];
  bossModel.viewer.choice_submitted = false;
  await app.loadView("boss", true);
  assert.equal(findClass(bossBody.children[0], "boss-action").children[0].textContent,
    "🛡 Голова");
  bossModel.battle.phase = "resolving";
  bossModel.available_actions = [];
  await app.loadView("boss", true);
  assert.equal(findClass(bossBody.children[0], "boss-action"), undefined);
  assert.ok(bossBody.children[0].children.some(node =>
    node.textContent.includes("Раунд разрешается")));
  const beforeWaitingPoll = bossFetchCount;
  now += 1500;
  intervals[2].callback();
  await new Promise(setImmediate);
  assert.equal(bossFetchCount, beforeWaitingPoll + 1);
  const beforeBossTick = bossFetchCount;
  document.hidden = true;
  intervals[2].callback();
  assert.equal(bossFetchCount, beforeBossTick);
  document.hidden = false;
  document.listeners.visibilitychange();
  await new Promise(setImmediate);
  assert.equal(bossFetchCount, beforeBossTick + 1);
  app.setContext("test-session", "duel");
  intervals[2].callback();
  assert.equal(bossFetchCount, beforeBossTick + 1);
}

testPolling().catch(error => { console.error(error); process.exitCode = 1; });
