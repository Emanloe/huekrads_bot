"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const app = fs.readFileSync(path.join(__dirname, "..", "miniapp_static", "app.js"), "utf8");
const html = fs.readFileSync(path.join(__dirname, "..", "miniapp_static", "index.html"), "utf8");
const telegramScript = html.match(/<script src="\/static\/vendor\/telegram-web-app\.js"[\s\S]*?<\/script>/);
assert.ok(telegramScript);
assert.ok(html.indexOf(telegramScript[0]) < html.indexOf('src="/static/app.js" defer'));
const eventHandlers = {
  load: telegramScript[0].match(/onload="([^"]+)"/)[1],
  error: telegramScript[0].match(/onerror="([^"]+)"/)[1],
};
const match = app.match(/  async function bootstrap\(\) \{[\s\S]*?\n  \}\n\n  bootstrap\(\);/);
assert.ok(match);
const bootstrapSource = match[0].replace(/\n\n  bootstrap\(\);$/, "\n  globalThis.runBootstrap = bootstrap;");

async function scenario({ webApp, scriptEvent = null, search = "", apiResult, homeError = false }) {
  const events = [];
  const messages = [];
  const calls = [];
  const telegramWindow = { Telegram: webApp === undefined ? undefined : { WebApp: webApp },
    location: { search } };
  if (scriptEvent) vm.runInNewContext(eventHandlers[scriptEvent], { window: telegramWindow });
  const sandbox = {
    URLSearchParams,
    window: telegramWindow,
    console: { warn: (...values) => events.push(values) },
    showUnavailable: (message, code) => messages.push({ message, code }),
    apiRequest: async (_path, options) => {
      calls.push(options.body);
      if (apiResult instanceof Error) throw apiResult;
      return apiResult;
    },
    loadView: async () => { if (homeError) throw new Error("home failure"); },
    loadRecap: async () => {},
    sessionToken: null,
    globalThis: null,
  };
  sandbox.globalThis = sandbox;
  vm.runInNewContext(`${bootstrapSource}\n`, sandbox);
  await sandbox.runBootstrap();
  return { events, messages, calls };
}

(async () => {
  const absent = await scenario({});
  assert.equal(absent.events[0][1].reason, "missing_init_data");
  assert.equal(absent.events[0][1].has_web_app, false);
  assert.equal(absent.calls.length, 0);
  assert.equal(absent.messages[0].code, "DBG: TG_SCRIPT_UNKNOWN_NO_WEBAPP");
  assert.ok(absent.messages[0].message.includes("/app"));

  const failedScript = await scenario({ scriptEvent: "error" });
  assert.equal(failedScript.messages[0].code, "DBG: TG_SCRIPT_ERROR");
  assert.equal(failedScript.calls.length, 0);

  const loadedWithoutWebApp = await scenario({ scriptEvent: "load" });
  assert.equal(loadedWithoutWebApp.messages[0].code, "DBG: TG_SCRIPT_LOADED_NO_WEBAPP");
  assert.equal(loadedWithoutWebApp.calls.length, 0);

  const empty = await scenario({ webApp: { initData: "", initDataUnsafe: {
    start_param: "SECRET_UNSAFE_START",
  } } });
  assert.equal(empty.events[0][1].reason, "missing_init_data");
  assert.equal(empty.events[0][1].has_web_app, true);
  assert.equal(empty.events[0][1].has_unsafe_start_param, true);
  assert.equal(empty.calls.length, 0);
  assert.equal(empty.messages[0].code, "DBG: EMPTY_INIT_DATA");

  const unsafeOnly = await scenario({ webApp: { initData: "auth_date=123",
    initDataUnsafe: { start_param: "SECRET_UNSAFE_START" }, ready() {}, expand() {},
  } });
  assert.equal(unsafeOnly.events[0][1].reason, "missing_launch_token");
  assert.equal(unsafeOnly.events[0][1].has_unsafe_start_param, true);
  assert.equal(unsafeOnly.calls.length, 0);
  assert.equal(unsafeOnly.messages[0].code, "DBG: NO_START_PARAM");

  const apiError = Object.assign(new Error("SECRET_SERVER_MESSAGE"), { status: 401 });
  const rejected = await scenario({ webApp: { initData: "auth_date=123",
    ready() {}, expand() {},
  }, search: "?tgWebAppStartParam=SECRET_QUERY_TOKEN", apiResult: apiError });
  assert.equal(rejected.calls[0].launch_token, "SECRET_QUERY_TOKEN");
  assert.equal(rejected.events[0][1].reason, "session_api_error");
  assert.equal(rejected.events[0][1].status, 401);
  assert.equal(rejected.messages.length, 1);
  assert.equal(rejected.messages[0].code, "DBG: SESSION_HTTP_401");
  assert.ok(!JSON.stringify(rejected.events).includes("SECRET_"));
  assert.ok(!JSON.stringify(rejected.messages).includes("SECRET_"));

  const home = await scenario({ webApp: { initData: "start_param=SECRET_INIT_TOKEN",
    ready() {}, expand() {},
  }, apiResult: { session_token: "SECRET_SESSION_TOKEN" }, homeError: true });
  assert.equal(home.calls[0].launch_token, "SECRET_INIT_TOKEN");
  assert.equal(home.events[0][1].reason, "post_session_load_error");
  assert.equal(home.messages[0].code, null);
  assert.ok(!JSON.stringify(home.events).includes("SECRET_"));
})().catch(error => { console.error(error); process.exitCode = 1; });
