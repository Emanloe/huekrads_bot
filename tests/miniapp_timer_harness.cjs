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
    this.classList = { toggle() {} };
  }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this.children = nodes; }
  addEventListener(name, callback) { this.listeners[name] = callback; }
  setAttribute() {}
  removeAttribute() {}
  get childElementCount() { return this.children.length; }
  querySelectorAll(selector) {
    const found = [];
    const visit = (node, inZone = false) => {
      const zone = inZone || node.className.split(" ").includes("zone-row");
      if (selector === ".zone-row button" && zone && node.tag === "button") found.push(node);
      for (const child of node.children) visit(child, zone);
    };
    visit(this);
    return found;
  }
}

const nodes = new Map();
for (const id of ["duel-content", "app-status", "refresh-button", "header-player"]) {
  nodes.set(id, new Node());
}
const intervals = [];
const timeouts = [];
let now = 0;
let wallOffset = 0;
let fetchCount = 0;
let moveCount = 0;
let moveMode = "accepted";
let serverDuel = null;
const document = {
  hidden: false,
  listeners: {},
  createElement: tag => new Node(tag),
  getElementById: id => nodes.get(id),
  querySelector: () => new Node(),
  querySelectorAll: selector => selector === ".zone-row button" ?
    nodes.get("duel-content").querySelectorAll(selector) : [],
  addEventListener(name, callback) { this.listeners[name] = callback; },
};
const window = {
  setInterval(callback, delay) { intervals.push({ callback, delay }); return intervals.length; },
  setTimeout(callback, delay) { timeouts.push({ callback, delay }); return timeouts.length; },
  clearTimeout() {},
};
const epoch = 1_700_000_000_000;
const fetch = async (url, options) => {
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
    renderDuel, updateCountdown, loadView, submitMove,
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
assert.deepEqual(intervals.map(item => item.delay), [1000, 250]);

const round = { round: 1, outcome: "hit", attacker: { display_name: "A" },
  defender: { display_name: "B" }, attack_zone: "head", defense_zone: "body" };
function duel(overrides = {}) {
  return {
    id: 1, turn_id: 2, round: 2, status: "active", phase: "attack",
    role: "attacker", can_act: true, deadline_at: epoch + 9600,
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
function countdownText() { return app.countdownTurn?.node.textContent; }

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

// The visual clock follows the server sample even if the device clock is wrong.
wallOffset = 60_000;
render(duel(), { serverNow: epoch + now, observedAt: now });
assert.equal(countdownText(), "7 сек.");
wallOffset = 0;

// A later turn replaces the old timer, including when rounds accumulate.
render(duel({ turn_id: 3, rounds: [round, { ...round, round: 2 }], deadline_at: epoch + 12500 }));
assert.equal(intervals.length, 2);
assert.equal(firstNode.textContent, "7 сек.");
now = 3650;
app.updateCountdown();
assert.equal(firstNode.textContent, "7 сек.");
assert.equal(countdownText(), "9 сек.");
const secondRoot = nodes.get("duel-content").children[0];
assert.ok(secondRoot.children.indexOf(findClass(secondRoot, "action-box")) <
  secondRoot.children.indexOf(findClass(secondRoot, "round-history")));

// Block and waiting phases keep controls above history, with correct disabled state.
render(duel({ turn_id: 4, phase: "block", role: "defender" }));
const blockRoot = nodes.get("duel-content").children[0];
assert.ok(blockRoot.children.indexOf(findClass(blockRoot, "action-box")) <
  blockRoot.children.indexOf(findClass(blockRoot, "round-history")));
render(duel({ turn_id: 5, phase: "block", role: "attacker", can_act: false }));
assert.ok(nodes.get("duel-content").querySelectorAll(".zone-row button").every(button => button.disabled));
render(duel({ turn_id: 6, rounds: [] }));
assert.equal(findClass(nodes.get("duel-content").children[0], "round-history"), undefined);

// At zero, only display/control state changes; a GET is scheduled once.
render(duel({ turn_id: 7, deadline_at: epoch + now + 100 }));
now += 150;
app.updateCountdown();
assert.equal(countdownText(), "Время вышло · ждём сервер");
assert.ok(nodes.get("duel-content").querySelectorAll(".zone-row button").every(button => button.disabled));
const scheduled = timeouts.filter(item => item.delay === 0).length;
app.updateCountdown();
assert.equal(timeouts.filter(item => item.delay === 0).length, scheduled);
assert.equal(fetchCount, 0);

// Finished/no active state has no countdown or action box.
render(null);
assert.equal(app.countdownTurn, null);
app.updateCountdown();
assert.equal(nodes.get("duel-content").querySelectorAll(".zone-row button").length, 0);
assert.equal(intervals.length, 2);
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
}

testPolling().catch(error => { console.error(error); process.exitCode = 1; });
