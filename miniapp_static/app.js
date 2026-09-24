"use strict";

(() => {
  const ACTIVE_POLL_MS = 1000;
  const IDLE_POLL_MS = 8000;
  const COUNTDOWN_TICK_MS = 250;
  const REQUEST_TIMEOUT_MS = 12000;
  const START_DUEL_PATH = "/api/v1/duel/start";
  const MOVE_DUEL_PATH = "/api/v1/duel/move";
  const API_PATHS = {
    home: "/api/v1/me",
    opponents: "/api/v1/duel/opponents",
    duel: "/api/v1/duel/active",
  };
  const ZONE_NAMES = { head: "Голова", body: "Торс", dick: "Хуй" };
  const PHASE_NAMES = { attack: "Атака", block: "Блок" };
  const ROLE_NAMES = { attacker: "Атакующий", defender: "Защищающийся", spectator: "Наблюдатель" };
  const OUTCOME_NAMES = { miss: "Промах", block: "Блок", hit: "Попадание", suicide: "Самопоражение" };

  let sessionToken = null;
  let currentView = "home";
  let activeDuel = null;
  let countdownNode = null;
  let countdownTurn = null;
  let duelPollStartedAt = -Infinity;
  let expiredDeadlineRefresh = null;
  let challengeInFlight = false;
  let moveInFlight = false;
  const loading = { home: null, opponents: null, duel: null };

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
    countdownTurn = null;
    refreshButton.disabled = true;
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

  function renderTitles(titles, compact = false) {
    const list = element("ul", compact ? "title-list compact" : "title-list");
    const categories = [
      ["wins", "Победы"], ["losses", "Поражения"],
      ["stolen_dicks", "Украденные хуи"],
    ];
    for (const [key, label] of categories) {
      const title = titles?.[key];
      if (compact && !title?.text) continue;
      const row = element("li");
      row.append(element("span", "title-name", title?.text || "Нет звания"),
        element("small", "title-count", `${label}: ${title?.count ?? 0}`));
      list.append(row);
    }
    if (!list.childElementCount) list.append(element("li", null, "Нет званий"));
    return list;
  }

  async function apiRequest(path, options = {}) {
    const headers = { Accept: "application/json" };
    if (options.method === "POST") headers["Content-Type"] = "application/json";
    if (sessionToken) headers.Authorization = `Bearer ${sessionToken}`;
    const controller = new AbortController();
    const timer = window.setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
    let response;
    const requestStartedAt = performance.now();
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
    const responseReceivedAt = performance.now();
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
    if (path === API_PATHS.duel) {
      const serverNow = Number(response.headers.get("X-Duel-Server-Time-Ms"));
      if (Number.isFinite(serverNow) && serverNow > 0) {
        // Count the whole request as elapsed so latency never promises extra time.
        data._clock = {
          serverNow: serverNow + Math.max(0, responseReceivedAt - requestStartedAt),
          observedAt: responseReceivedAt,
        };
      }
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
      dataCell("Очки", `${data.points} / ${data.max_points}`),
      dataCell("Победы / поражения", `${data.wins} / ${data.losses}`),
      dataCell("Побед сегодня", data.daily_wins),
      dataCell("Участие в дуэли", data.ineligibility === "no_dick" ? "Недоступно до завтра" : "Доступно")
    );
    content.append(grid);
    addHeading(content, "Хуяние");
    content.append(renderTitles(data.titles));
    addHeading(content, "Статус");
    const status = element("div", "data-grid");
    status.append(
      dataCell("Побеждено боссов", data.boss_wins),
      dataCell("Статус на сегодня", data.dick_status?.text || "—")
    );
    content.append(status);
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
    if (data.pet) content.append(notice(data.pet));
    body.replaceChildren(content);
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
      content.append(element("p", "hint", "Соперники из текущего чата. Ходить можно здесь или в Telegram."));
      const head = element("div", "opponent-head");
      head.append(element("span", null, "Гном"), element("span", null, "Действие"));
      const list = element("div", "opponent-list");
      for (const opponent of data.opponents) {
        const row = element("div", "opponent-row");
        const identity = element("div", "opponent-name", opponent.title || opponent.username || "Соперник");
        if (opponent.username) identity.append(element("span", "opponent-handle", `@${opponent.username}`));
        identity.append(element("span", "opponent-stats",
          `Очки: ${opponent.points} · Победы: ${opponent.wins} · Поражения: ${opponent.losses}`));
        identity.append(renderTitles(opponent.titles, true));
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
        setStatus("Дуэль начата. Ходите в Mini App или Telegram.");
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

  function renderRoundHistory(rounds) {
    const history = element("section", "round-history");
    addHeading(history, "Завершённые раунды");
    const list = element("div", "round-list");
    for (const round of rounds) {
      const entry = element("div", "round-result");
      entry.append(element("strong", null, `Раунд ${round.round} · ${OUTCOME_NAMES[round.outcome] || "Итог раунда"}`));
      entry.append(
        element("div", null, `${round.attacker.display_name} — атака: ${ZONE_NAMES[round.attack_zone] || "—"}`),
        element("div", null, `${round.defender.display_name} — защита: ${ZONE_NAMES[round.defense_zone] || "—"}`)
      );
      if (round.presentation_text) entry.append(element("p", "round-prose", round.presentation_text));
      else if (round.outcome_text) entry.append(element("small", null, round.outcome_text));
      for (const timeoutText of round.timeout_texts || []) {
        entry.append(element("small", "duel-presentation", timeoutText));
      }
      list.append(entry);
    }
    history.append(list);
    return history;
  }

  function renderFinished(finished) {
    const content = element("section", "duel-finished");
    addHeading(content, `Дуэль №${finished.id} завершена`);
    content.append(element("p", "hint", `${finished.player1.display_name} против ${finished.player2.display_name}`));
    const points = finished.points;
    const signed = value => value > 0 ? `+${value}` : String(value);
    const pointLine = (before, after, awarded) =>
      `${before} → ${after}${Number.isInteger(awarded) ? ` (${signed(awarded)})` : ""}`;
    const grid = element("div", "data-grid");
    grid.append(
      dataCell("Победитель", finished.winner.display_name),
      dataCell("Проигравший", finished.loser.display_name),
      dataCell("Очки победителя", pointLine(points.winner_before, points.winner_after, points.winner_delta_awarded)),
      dataCell("Очки проигравшего", pointLine(points.loser_before, points.loser_after, points.loser_delta_awarded))
    );
    if (finished.duration?.text) grid.append(dataCell("Длительность", finished.duration.text));
    content.append(grid);
    if (finished.stolen_item) content.append(notice(`Украден предмет: ${finished.stolen_item.name}.`));
    if (finished.dick_stolen) content.append(notice("У проигравшего украден хуй."));
    if (finished.round_flavor) content.append(element("p", "notice duel-presentation", finished.round_flavor));
    if (finished.dwarf_fact) content.append(element("p", "notice duel-presentation", `📖 ${finished.dwarf_fact}`));
    if (finished.berserk) {
      content.append(notice(finished.berserk.text || (finished.berserk.dick_lost ?
        `Берсерк ${finished.berserk.berserker.display_name} откусил хуй ${finished.berserk.victim.display_name}.` :
        `Берсерк ${finished.berserk.berserker.display_name} набросился на ${finished.berserk.victim.display_name}, но хуй уже был украден.`)));
    }
    if (finished.post_message) {
      content.append(element("p", "notice duel-presentation",
        `${finished.note_prefix || "Заметка:"}\n${finished.post_message}`));
    }
    if (Array.isArray(finished.rounds) && finished.rounds.length) {
      content.append(renderRoundHistory(finished.rounds));
    }
    const opponents = element("button", "small-button", "К соперникам");
    opponents.type = "button";
    opponents.addEventListener("click", () => navigate("opponents"));
    content.append(opponents);
    return content;
  }

  function renderDuel(data) {
    const body = document.getElementById("duel-content");
    activeDuel = data.duel || null;
    countdownNode = null;
    countdownTurn = null;
    if (!activeDuel) {
      body.replaceChildren(data.recent_finished ? renderFinished(data.recent_finished) :
        notice("Сейчас в этом чате нет активной дуэли."));
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
      dataCell("Ваш ход", duel.can_act ? "Да — Mini App или Telegram" : "Нет"),
      dataCell("Ход", duel.turn_id)
    );
    if (duel.attack_zone && Object.prototype.hasOwnProperty.call(ZONE_NAMES, duel.attack_zone)) {
      grid.append(dataCell("Выбранная зона атаки", ZONE_NAMES[duel.attack_zone]));
    }
    if (Number.isFinite(duel.deadline_at)) {
      countdownNode = element("span", "value");
      const clock = data._clock || { serverNow: Date.now(), observedAt: performance.now() };
      countdownTurn = {
        duelId: duel.id, turnId: duel.turn_id, deadlineAt: duel.deadline_at,
        node: countdownNode, serverNow: clock.serverNow, observedAt: clock.observedAt,
      };
      const deadlineCell = element("div", "data-cell");
      deadlineCell.append(element("span", "label", "До конца хода"), countdownNode);
      grid.append(deadlineCell);
    }
    content.append(grid);

    const attackAccepted = duel.role === "attacker" && duel.phase === "block" &&
      duel.own_attack_accepted === true;
    if (duel.status === "publishing" && !attackAccepted) {
      const justResolved = duel.rounds?.some(round => round.resolved_turn_id === duel.turn_id - 1);
      content.append(notice(justResolved ? "Раунд разрешён. Ожидаем публикацию итога…" :
        "Публикуем следующий ход в Telegram…"));
    }

    if ((duel.status === "active" && (duel.role === "attacker" || duel.role === "defender")) ||
        (duel.status === "publishing" && attackAccepted)) {
      const canChoose = duel.can_act && Number.isFinite(duel.deadline_at) &&
        remainingCountdownMs() > 0 && !moveInFlight;
      const waitingForAttack = duel.role === "defender" && duel.phase === "attack";
      const actions = element("div", "action-box");
      actions.append(
        element("h3", null, canChoose ? (duel.phase === "attack" ? "Атака" : "Блок") :
          attackAccepted ? "Атака" : "Ожидание"),
        element("p", null, canChoose ? "Выберите зону хода." :
          attackAccepted ? "Атака принята. Ожидаем соперника…" :
            waitingForAttack ? "Соперник выбирает зону атаки…" :
              "Ожидаем сервер или другого участника.")
      );
      if (canChoose) {
        const zones = element("div", "zone-row");
        for (const [zone, label] of Object.entries(ZONE_NAMES)) {
          const button = element("button", "small-button", label);
          button.type = "button";
          button.addEventListener("click", () => submitMove(zone));
          zones.append(button);
        }
        actions.append(zones);
      }
      content.append(actions);
    }
    if (Array.isArray(duel.rounds) && duel.rounds.length) {
      content.append(renderRoundHistory(duel.rounds));
    }
    if (data.recent_finished && data.recent_finished.id !== duel.id) {
      const previous = element("details", "previous-duel");
      previous.append(element("summary", null, "Последняя завершённая дуэль"),
        renderFinished(data.recent_finished));
      content.append(previous);
    }
    body.replaceChildren(content);
    updateCountdown();
  }

  async function submitMove(zone) {
    const duel = activeDuel;
    if (!sessionToken || moveInFlight || !duel?.can_act ||
        !Number.isFinite(duel.deadline_at) || remainingCountdownMs() <= 0) return;
    moveInFlight = true;
    for (const button of document.querySelectorAll(".zone-row button")) button.disabled = true;
    setStatus("Передаём выбор…");
    let accepted = false;
    let failure = null;
    try {
      await apiRequest(MOVE_DUEL_PATH, {
        method: "POST", body: { duel_id: duel.id, turn_id: duel.turn_id, zone },
      });
      accepted = true;
    } catch (error) {
      if (error.status === 401) {
        showUnavailable(error.message);
      } else {
        failure = error.message || "Не удалось выполнить ход.";
      }
    }
    // A timed-out HTTP response may still follow a committed move. Wait for any
    // older poll, then fetch the authoritative turn before enabling controls.
    if (loading.duel) await loading.duel;
    moveInFlight = false;
    if (!sessionToken) return;
    const refreshed = await loadView("duel", true);
    if (!refreshed) {
      setStatus("Не удалось проверить состояние дуэли. Обновите экран перед новым ходом.", true);
    } else if (accepted) {
      setStatus(activeDuel ? "Выбор принят. Дальше ждём состояние сервера." :
        "Результат дуэли обновлён.");
    } else {
      setStatus(failure || "Проверьте состояние дуэли перед повторным выбором.", true);
    }
  }

  function remainingCountdownMs() {
    const turn = countdownTurn;
    if (!turn || !Number.isFinite(turn.deadlineAt) || !Number.isFinite(turn.serverNow)) return 0;
    const elapsed = Math.max(0, performance.now() - turn.observedAt);
    return Math.max(0, turn.deadlineAt - turn.serverNow - elapsed);
  }

  function updateCountdown() {
    const turn = countdownTurn;
    if (currentView !== "duel" || document.hidden || !activeDuel || !turn ||
        turn.node !== countdownNode || turn.duelId !== activeDuel.id ||
        turn.turnId !== activeDuel.turn_id || turn.deadlineAt !== activeDuel.deadline_at) return;
    const remaining = Math.ceil(remainingCountdownMs() / 1000);
    countdownNode.textContent = remaining > 0 ? `${remaining} сек.` : "Время вышло · ждём сервер";
    if (remaining === 0) {
      for (const button of document.querySelectorAll(".zone-row button")) button.disabled = true;
    }
    if (remaining === 0 && sessionToken) {
      const key = `${activeDuel.id}:${activeDuel.turn_id}:${activeDuel.deadline_at}`;
      if (expiredDeadlineRefresh !== key) {
        expiredDeadlineRefresh = key;
        // Let the current response finish before the one authoritative refresh.
        window.setTimeout(() => {
          if (countdownTurn === turn && !document.hidden && currentView === "duel") {
            loadView("duel", true);
          }
        }, 0);
      }
    }
  }

  async function loadView(view, silent = false) {
    if (!sessionToken) return false;
    if (loading[view]) return loading[view];
    if (view === "duel") duelPollStartedAt = performance.now();
    const request = (async () => {
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
      }
    })();
    loading[view] = request;
    try { return await request; }
    finally { if (loading[view] === request) loading[view] = null; }
  }

  function navigate(view) {
    currentView = view;
    if (view !== "duel") {
      countdownNode = null;
      countdownTurn = null;
    }
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
    if (document.hidden || currentView !== "duel") return;
    updateCountdown();
    // An older request may have started before the WebView was backgrounded.
    // Finish it first, then fetch a fresh authoritative state on return.
    if (loading.duel) {
      loading.duel.then(() => {
        window.setTimeout(() => {
          if (!document.hidden && currentView === "duel") loadView("duel", true);
        }, 0);
      });
    } else {
      loadView("duel", true);
    }
  });
  window.setInterval(() => {
    if (!document.hidden && currentView === "duel" && sessionToken &&
        performance.now() - duelPollStartedAt >= (activeDuel ? ACTIVE_POLL_MS : IDLE_POLL_MS)) {
      loadView("duel", true);
    }
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
