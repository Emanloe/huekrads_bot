"use strict";

(() => {
  const ACTIVE_POLL_MS = 8000;
  const COUNTDOWN_TICK_MS = 1000;
  const REQUEST_TIMEOUT_MS = 12000;
  const START_DUEL_PATH = "/api/v1/duel/start";
  const API_PATHS = {
    home: "/api/v1/me",
    opponents: "/api/v1/duel/opponents",
    duel: "/api/v1/duel/active",
  };
  const ZONE_NAMES = { head: "Голова", body: "Торс", dick: "Хуй" };
  const PHASE_NAMES = { attack: "Атака", block: "Блок" };
  const ROLE_NAMES = { attacker: "Атакующий", defender: "Защищающийся", spectator: "Наблюдатель" };

  let sessionToken = null;
  let currentView = "home";
  let activeDuel = null;
  let countdownNode = null;
  let expiredDeadlineRefresh = null;
  let challengeInFlight = false;
  const loading = { home: false, opponents: false, duel: false };

  const statusNode = document.getElementById("app-status");
  const toolbar = document.querySelector(".toolbar");
  const refreshButton = document.getElementById("refresh-button");

  function element(tag, className, value) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (value !== undefined && value !== null) node.textContent = String(value);
    return node;
  }

  function setStatus(message, error = false) {
    statusNode.textContent = message;
    toolbar.classList.toggle("is-error", error);
  }

  function showUnavailable(message) {
    sessionToken = null;
    activeDuel = null;
    countdownNode = null;
    refreshButton.disabled = true;
    document.getElementById("header-player").textContent = "Гном не загружен";
    for (const view of Object.keys(API_PATHS)) {
      document.getElementById(`screen-${view}`).querySelector(".panel-body").replaceChildren(
        element("p", "empty-content", "Данные доступны после открытия из Telegram.")
      );
    }
    setStatus(message, true);
  }

  function dataCell(label, value) {
    const cell = element("div", "data-cell");
    cell.append(element("span", "label", label), element("span", "value", value));
    return cell;
  }

  function addHeading(parent, title) {
    parent.append(element("h3", "subheading", title));
  }

  function notice(message, bad = false) {
    return element("p", bad ? "notice bad" : "notice", message);
  }

  async function apiRequest(path, options = {}) {
    const headers = { Accept: "application/json" };
    if (options.method === "POST") headers["Content-Type"] = "application/json";
    if (sessionToken) headers.Authorization = `Bearer ${sessionToken}`;
    const controller = new AbortController();
    const timer = window.setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
    let response;
    try {
      response = await fetch(path, {
        method: options.method || "GET",
        headers,
        body: options.body === undefined ? undefined : JSON.stringify(options.body),
        signal: controller.signal,
        cache: "no-store",
        credentials: "same-origin",
      });
    } catch {
      throw new Error("Нет связи с сервером. Обновите данные перед повторной попыткой.");
    } finally {
      window.clearTimeout(timer);
    }
    let data;
    try {
      if (!response.headers.get("content-type")?.includes("application/json")) throw new Error();
      data = await response.json();
    } catch {
      throw new Error("Сервер вернул неожиданный ответ. Попробуйте обновить данные.");
    }
    if (!response.ok) {
      const detail = data && typeof data.detail === "object" ? data.detail : null;
      const error = new Error(
        response.status === 401 ? "Сессия или ссылка истекла. Откройте /duel_app в чате ещё раз." :
        typeof detail?.message === "string" ? detail.message :
        response.status === 404 ? "Гном не найден в этом чате." :
        "Не удалось получить данные. Попробуйте обновить экран."
      );
      error.status = response.status;
      error.code = detail?.code;
      throw error;
    }
    return data;
  }

  function renderHome(data) {
    const body = document.getElementById("home-content");
    const content = element("div");
    addHeading(content, "Гном");
    const grid = element("div", "data-grid");
    grid.append(
      dataCell("Имя", data.display_name || data.username || "Без имени"),
      dataCell("Имя гнома", data.dwarf_name || "Не задано"),
      dataCell("Очки", data.points),
      dataCell("Победы / поражения", `${data.wins} / ${data.losses}`),
      dataCell("Побед сегодня", data.daily_wins),
      dataCell("Участие в дуэли", data.ineligibility === "no_dick" ? "Недоступно до завтра" : "Доступно")
    );
    content.append(grid);
    addHeading(content, "Инвентарь");
    if (!Array.isArray(data.inventory) || data.inventory.length === 0) {
      content.append(notice("В инвентаре пока нет предметов."));
    } else {
      const inventory = element("ul", "inventory-list");
      for (const item of data.inventory) {
        const row = element("li");
        row.append(element("span", null, item.name || item.item_id), element("strong", null, `×${item.count}`));
        inventory.append(row);
      }
      content.append(inventory);
    }
    body.replaceChildren(content);
    document.getElementById("header-player").textContent = data.dwarf_name || data.display_name || data.username || "Гном";
  }

  function renderOpponents(data) {
    const body = document.getElementById("opponents-content");
    const content = element("div");
    if (data.ineligibility === "no_dick") {
      content.append(notice("Сегодня дуэли недоступны: у вашего гнома нет хуя.", true));
    } else if (data.ineligibility) {
      content.append(notice("Список соперников сейчас недоступен.", true));
    } else if (!Array.isArray(data.opponents) || data.opponents.length === 0) {
      content.append(notice("В этом чате сейчас нет доступных соперников."));
    } else {
      content.append(element("p", "hint", "Соперники из текущего чата. Ходы пока выполняются в Telegram."));
      const head = element("div", "opponent-head");
      head.append(element("span", null, "Гном"), element("span", null, "Действие"));
      const list = element("div", "opponent-list");
      for (const opponent of data.opponents) {
        const row = element("div", "opponent-row");
        const identity = element("div", "opponent-name", opponent.title || opponent.username || "Соперник");
        if (opponent.username) identity.append(element("span", "opponent-handle", `@${opponent.username}`));
        const action = element("button", "small-button", "Вызвать");
        action.type = "button";
        action.disabled = challengeInFlight;
        action.addEventListener("click", () => challengeOpponent(opponent.user_id));
        row.append(identity, action);
        list.append(row);
      }
      content.append(head, list);
    }
    body.replaceChildren(content);
  }

  async function challengeOpponent(opponentUserId) {
    if (!sessionToken || challengeInFlight) return;
    challengeInFlight = true;
    for (const button of document.querySelectorAll(".opponent-list button")) button.disabled = true;
    setStatus("Начинаем дуэль…");
    try {
      await apiRequest(START_DUEL_PATH, {
        method: "POST", body: { opponent_user_id: opponentUserId },
      });
      if (await navigate("duel")) {
        setStatus("Дуэль начата. Ходы выполняются в Telegram.");
      }
    } catch (error) {
      if (error.status === 401) {
        showUnavailable(error.message);
      } else {
        await Promise.all([loadView("opponents", true), loadView("duel", true)]);
        setStatus(error.message || "Не удалось начать дуэль. Обновите данные.", true);
      }
    } finally {
      challengeInFlight = false;
      for (const button of document.querySelectorAll(".opponent-list button")) button.disabled = false;
    }
  }

  function renderDuel(data) {
    const body = document.getElementById("duel-content");
    activeDuel = data.duel || null;
    countdownNode = null;
    if (!activeDuel) {
      body.replaceChildren(notice("Сейчас в этом чате нет активной дуэли."));
      return;
    }
    const duel = activeDuel;
    const content = element("div");
    content.append(element("p", "duel-heading", `Дуэль №${duel.id} · раунд ${duel.round}`));
    const versus = element("div", "duel-versus");
    const attacker = element("div", "duel-player", duel.attacker?.display_name || "Атакующий");
    attacker.append(element("small", null, "Атакующий"));
    const defender = element("div", "duel-player", duel.defender?.display_name || "Защищающийся");
    defender.append(element("small", null, "Защищающийся"));
    versus.append(attacker, element("span", "versus", "VS"), defender);
    content.append(versus);

    const grid = element("div", "data-grid");
    grid.append(
      dataCell("Состояние", duel.status === "active" ? "Активна" : "Публикация хода"),
      dataCell("Фаза", PHASE_NAMES[duel.phase] || duel.phase || "—"),
      dataCell("Сейчас ходит", duel.phase === "attack" ? duel.attacker?.display_name : duel.defender?.display_name),
      dataCell("Ваша роль", ROLE_NAMES[duel.role] || "Наблюдатель"),
      dataCell("Ваш ход", duel.can_act ? "Да — через Telegram" : "Нет"),
      dataCell("Ход", duel.turn_id)
    );
    if (duel.attack_zone && Object.prototype.hasOwnProperty.call(ZONE_NAMES, duel.attack_zone)) {
      grid.append(dataCell("Выбранная зона атаки", ZONE_NAMES[duel.attack_zone]));
    }
    if (Number.isFinite(duel.deadline_at)) {
      countdownNode = element("span", "value");
      const deadlineCell = element("div", "data-cell");
      deadlineCell.append(element("span", "label", "До конца хода"), countdownNode);
      grid.append(deadlineCell);
    }
    content.append(grid);

    if (duel.status === "active" && (duel.phase === "attack" || duel.phase === "block")) {
      const actions = element("div", "action-box");
      actions.append(
        element("h3", null, duel.phase === "attack" ? "Атака" : "Блок"),
        element("p", null, "Кнопки для будущего этапа. Сейчас ход выполняется в Telegram.")
      );
      const zones = element("div", "zone-row");
      for (const zone of Object.values(ZONE_NAMES)) {
        const button = element("button", "readonly-button", zone);
        button.type = "button";
        button.disabled = true;
        zones.append(button);
      }
      actions.append(zones);
      content.append(actions);
    }
    body.replaceChildren(content);
    updateCountdown();
  }

  function updateCountdown() {
    if (!activeDuel || !countdownNode || !Number.isFinite(activeDuel.deadline_at)) return;
    const remaining = Math.max(0, Math.ceil((activeDuel.deadline_at - Date.now()) / 1000));
    countdownNode.textContent = remaining > 0 ? `${remaining} сек.` : "Время вышло · ждём сервер";
    if (remaining === 0 && currentView === "duel" && !document.hidden && sessionToken) {
      const key = `${activeDuel.id}:${activeDuel.turn_id}:${activeDuel.deadline_at}`;
      if (expiredDeadlineRefresh !== key) {
        expiredDeadlineRefresh = key;
        loadView("duel", true);
      }
    }
  }

  async function loadView(view, silent = false) {
    if (!sessionToken || loading[view]) return;
    loading[view] = true;
    if (!silent) setStatus("Загрузка данных…");
    try {
      const data = await apiRequest(API_PATHS[view]);
      if (view === "home") renderHome(data);
      else if (view === "opponents") renderOpponents(data);
      else renderDuel(data);
      if (!silent) setStatus("Данные обновлены");
      return true;
    } catch (error) {
      if (error.status === 401) showUnavailable(error.message);
      else setStatus(error.message || "Не удалось загрузить данные.", true);
      return false;
    } finally {
      loading[view] = false;
    }
  }

  function navigate(view) {
    currentView = view;
    for (const tab of document.querySelectorAll(".tab")) {
      const active = tab.dataset.view === view;
      tab.classList.toggle("is-active", active);
      if (active) tab.setAttribute("aria-current", "page");
      else tab.removeAttribute("aria-current");
    }
    for (const section of document.querySelectorAll(".screen")) {
      section.hidden = section.id !== `screen-${view}`;
    }
    return loadView(view);
  }

  for (const tab of document.querySelectorAll(".tab")) {
    tab.addEventListener("click", () => navigate(tab.dataset.view));
  }
  refreshButton.addEventListener("click", () => loadView(currentView));
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden && currentView === "duel") loadView("duel", true);
  });
  window.setInterval(() => {
    if (!document.hidden && currentView === "duel") loadView("duel", true);
  }, ACTIVE_POLL_MS);
  window.setInterval(updateCountdown, COUNTDOWN_TICK_MS);

  async function bootstrap() {
    const webApp = window.Telegram?.WebApp;
    if (!webApp || !webApp.initData) {
      showUnavailable("Откройте приложение через кнопку /duel_app в игровом чате Telegram.");
      return;
    }
    webApp.ready();
    webApp.expand();
    const launchToken = new URLSearchParams(webApp.initData).get("start_param") ||
      new URLSearchParams(window.location.search).get("tgWebAppStartParam");
    if (!launchToken) {
      showUnavailable("Нужна ссылка из игрового чата. Вызовите там /duel_app ещё раз.");
      return;
    }
    try {
      const created = await apiRequest("/api/v1/session", {
        method: "POST", body: { init_data: webApp.initData, launch_token: launchToken },
      });
      if (typeof created.session_token !== "string" || !created.session_token) throw new Error();
      sessionToken = created.session_token;
      refreshButton.disabled = false;
      await loadView("home");
    } catch {
      showUnavailable("Ссылка уже использована или устарела. Вернитесь в чат и вызовите /duel_app ещё раз.");
    }
  }

  bootstrap();
})();
