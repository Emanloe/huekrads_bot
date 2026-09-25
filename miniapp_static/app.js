"use strict";

(() => {
  const ACTIVE_POLL_MS = 1000;
  const IDLE_POLL_MS = 8000;
  const BOSS_POLL_MS = 1000;
  const BOSS_WAIT_POLL_MS = 1500;
  const BOSS_IDLE_POLL_MS = 8000;
  const COUNTDOWN_TICK_MS = 250;
  const REQUEST_TIMEOUT_MS = 12000;
  const START_DUEL_PATH = "/api/v1/duel/start";
  const MOVE_DUEL_PATH = "/api/v1/duel/move";
  const API_PATHS = {
    home: "/api/v1/me",
    opponents: "/api/v1/duel/opponents",
    duel: "/api/v1/duel/active",
    hall: "/api/v1/duel/hall-of-fame",
    boss: "/api/v1/boss",
  };
  const ZONE_NAMES = { head: "Голова", body: "Торс", dick: "Хуй" };
  const PHASE_NAMES = { attack: "Атака", block: "Блок" };
  const ROLE_NAMES = { attacker: "Атакующий", defender: "Защищающийся", spectator: "Наблюдатель" };
  const OUTCOME_NAMES = { miss: "Промах", block: "Блок", hit: "Попадание", suicide: "Самопоражение" };

  let sessionToken = null;
  let currentView = "home";
  let activeDuel = null;
  let bossSnapshot = null;
  let bossCountdown = null;
  let bossPollStartedAt = -Infinity;
  let bossExpiredKey = null;
  let bossActionInFlight = false;
  let countdownNode = null;
  let countdownTurn = null;
  let duelPollStartedAt = -Infinity;
  let expiredDeadlineRefresh = null;
  let challengeInFlight = false;
  let inspectedPlayerId = null;
  let inspectedPlayerSource = null;
  let opponentsSnapshot = null;
  let hallSnapshot = null;
  let moveInFlight = false;
  const loading = { home: null, opponents: null, duel: null, hall: null, boss: null };

  const viewStatuses = { home: null, opponents: null, duel: null, hall: null, boss: null };

  function element(tag, className, value) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (value !== undefined && value !== null) node.textContent = String(value);
    return node;
  }

  function setViewStatus(view, message, error = false, kind = "read") {
    let state = viewStatuses[view];
    if (!state) {
      const node = notice(message, error);
      node.className += " view-message";
      const screen = document.getElementById(`screen-${view}`);
      screen.insertBefore(node, screen.querySelector(".panel-body"));
      state = { node, kind };
      viewStatuses[view] = state;
    } else {
      state.node.className = error ? "notice bad view-message" : "notice view-message";
      state.node.textContent = message;
      state.kind = kind;
    }
  }

  function clearViewStatus(view, kind = null) {
    const state = viewStatuses[view];
    if (state && (!kind || state.kind === kind)) {
      state.node.remove();
      viewStatuses[view] = null;
    }
  }

  function showUnavailable(message) {
    sessionToken = null;
    activeDuel = null;
    bossSnapshot = null;
    bossCountdown = null;
    countdownNode = null;
    countdownTurn = null;
    inspectedPlayerId = null;
    inspectedPlayerSource = null;
    for (const view of Object.keys(API_PATHS)) {
      clearViewStatus(view);
      document.getElementById(`screen-${view}`).querySelector(".panel-body").replaceChildren(
        notice(message, true)
      );
    }
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
    if (path === API_PATHS.duel || path === API_PATHS.boss) {
      const serverNow = Number(response.headers.get(
        path === API_PATHS.boss ? "X-Boss-Server-Time-Ms" : "X-Duel-Server-Time-Ms"
      ));
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

  function renderProfile(data, body, inspected = false, returnView = "opponents") {
    const content = element("div");
    if (inspected) {
      const back = element("button", "small-button inspect-back",
        returnView === "hall" ? "← К залу славы" : "← К соперникам");
      back.type = "button";
      back.addEventListener("click", () => {
        inspectedPlayerId = null;
        inspectedPlayerSource = null;
        const snapshot = returnView === "hall" ? hallSnapshot : opponentsSnapshot;
        if (snapshot) {
          if (returnView === "hall") renderHall(snapshot);
          else renderOpponents(snapshot);
        } else loadView(returnView);
      });
      content.append(back, element("p", "hint", "Профиль игрока · только просмотр"));
    }
    const grid = element("div", "data-grid");
    grid.append(
      dataCell("Имя", data.display_name || data.username || "Без имени"),
      dataCell("Имя гнома", data.dwarf_name || "Не задано"),
      dataCell("Очки", `${data.points} / ${data.max_points}`),
      dataCell("Победы / поражения", `${data.wins} / ${data.losses}`),
      dataCell("Побед сегодня", data.daily_wins),
      dataCell("Участие в дуэли", data.ineligibility === "no_dick" ? "Недоступно до завтра" : "Доступно")
    );
    if (inspected) grid.append(dataCell("Telegram", data.username ? `@${data.username}` : "Не указан"));
    const profile = element("div", "home-profile");
    const image = element("img", "gnome-image");
    image.src = data.gnome_image_url || document.getElementById("screen-home").dataset.gnomeSrc;
    image.alt = "Гном";
    image.width = 200;
    image.height = 200;
    image.decoding = "async";
    grid.classList.add("home-profile-info");
    profile.append(image, grid);
    content.append(profile);
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

  function renderHome(data) {
    renderProfile(data, document.getElementById("home-content"));
  }

  async function inspectOpponent(userId, sourceView = "opponents") {
    if (!sessionToken || challengeInFlight) return;
    inspectedPlayerId = userId;
    inspectedPlayerSource = sourceView;
    setViewStatus(sourceView, "Загрузка профиля…");
    try {
      const data = await apiRequest(`/api/v1/players/${encodeURIComponent(userId)}`);
      if (!sessionToken || inspectedPlayerId !== userId || inspectedPlayerSource !== sourceView) return;
      renderProfile(data, document.getElementById(`${sourceView}-content`), true, sourceView);
      clearViewStatus(sourceView, "read");
    } catch (error) {
      if (error.status === 401) showUnavailable(error.message);
      else if (sessionToken && inspectedPlayerId === userId && inspectedPlayerSource === sourceView) {
        inspectedPlayerId = null;
        inspectedPlayerSource = null;
        setViewStatus(sourceView, error.message || "Не удалось загрузить профиль.", true);
      }
    }
  }

  function renderHall(data) {
    hallSnapshot = data;
    if (inspectedPlayerId !== null && inspectedPlayerSource === "hall") return;
    const body = document.getElementById("hall-content");
    const content = element("div");
    if (!Array.isArray(data.players) || data.players.length === 0) {
      content.append(notice("Таблица лидеров чата пока пуста."));
    } else {
      content.append(element("p", "hint", "Топ-10 гномьих дуэлянтов чата · по победам"));
      const list = element("div", "hall-list");
      for (const player of data.players) {
        const row = element("div", "hall-row");
        const image = element("img", "hall-avatar");
        image.src = player.gnome_image_url;
        image.alt = "";
        image.width = 36;
        image.height = 36;
        image.decoding = "async";
        const identity = element("div", "hall-player");
        identity.append(element("strong", null, player.title),
          element("small", null, `${player.points} очков · ${player.wins}W / ${player.losses}L`));
        const inspect = element("button", "small-button", "Осмотреть");
        inspect.type = "button";
        inspect.addEventListener("click", () => inspectOpponent(player.user_id, "hall"));
        row.append(element("span", "hall-rank", `${player.rank}.`), image, identity, inspect);
        list.append(row);
      }
      content.append(list);
    }
    body.replaceChildren(content);
  }

  function renderBossResult(result) {
    const section = element("section", "boss-result");
    addHeading(section, "Итоги последней битвы");
    const banner = element("div", "boss-banner");
    banner.append(element("span", "boss-emoji", "⚔"),
      element("strong", "boss-identity", result.boss.name));
    section.append(banner);
    const victory = result.outcome === "victory";
    section.append(element("strong", victory ?
      "boss-result-outcome victory" : "boss-result-outcome defeat",
      victory ? "ПОБЕДА" : "ПОРАЖЕНИЕ"));
    const grid = element("div", "data-grid");
    grid.append(
      dataCell("Попадания", `${result.hits} / ${result.required_hits}`),
      dataCell("Раундов", result.rounds),
      dataCell("Участников", result.participants_count),
      dataCell("Выжили", result.alive_count),
      dataCell("Выбыли", result.participants_count - result.alive_count)
    );
    if (result.hero) {
      grid.append(dataCell("Герой битвы", result.hero.title));
      grid.append(dataCell("Попадания героя", result.hero.hits));
      grid.append(dataCell("Блоки героя", result.hero.blocks));
      if (victory) grid.append(dataCell("Раундов героя", result.hero.rounds_survived));
    }
    section.append(grid);
    addHeading(section, "Ваш результат");
    const viewer = result.viewer || {};
    let own = "Вы не участвовали в этой битве.";
    if (viewer.participated) {
      own = viewer.alive ? "Вы выжили." : "Вы выбыли.";
      own += ` Попаданий: ${viewer.hits ?? 0}.`;
      own += ` Блоков: ${viewer.blocks ?? 0}. Раундов: ${viewer.rounds_survived ?? 0}.`;
      if (viewer.rewarded) own += " Награда получена: 100 очков и восстановление.";
      if (viewer.received_item) own += " Вы получили предмет.";
    }
    section.append(notice(own));
    if (result.item_loot) {
      section.append(notice(
        `Предмет: ${result.item_loot.item_name} — ${result.item_loot.recipient_title}.`
      ));
    }
    if (Array.isArray(result.participants) && result.participants.length) {
      addHeading(section, "Участники");
      const team = element("ul", "boss-team");
      for (const participant of result.participants) {
        const row = element("li");
        row.append(element("span", "boss-team-name", participant.title),
          element("span", "boss-team-state", participant.alive ?
            `Выжил · попаданий: ${participant.hits}` :
            `Выбыл · попаданий: ${participant.hits}`));
        team.append(row);
      }
      section.append(team);
    }
    return section;
  }

  function renderBoss(data) {
    const body = document.getElementById("boss-content");
    const content = element("div");
    const battle = data.battle;
    bossSnapshot = data;
    bossCountdown = null;
    if (!battle) {
      content.append(data.recent_result ?
        renderBossResult(data.recent_result) :
        notice("Сейчас битвы с боссом нет."));
      addHeading(content, "Запись на бой");
      const registration = data.registration || {};
      content.append(notice(registration.open ?
        "Запись до 13:37 по Москве открыта в Telegram через /boss_reg." :
        "Запись на сегодняшний бой закрыта."));
      const grid = element("div", "data-grid");
      grid.append(
        dataCell("Записано участников", registration.participants_count ?? 0),
        dataCell("Моя запись", registration.viewer_registered ? "Вы записаны" : "Вы не записаны")
      );
      content.append(grid);
      body.replaceChildren(content);
      return;
    }

    const banner = element("div", "boss-banner");
    const identity = element("div", "boss-identity");
    identity.append(element("strong", null, battle.boss.name),
      element("small", null, battle.boss.description));
    banner.append(element("span", "boss-emoji", battle.boss.emoji), identity);
    content.append(banner);
    if (battle.phase !== "join") {
      const progress = element("progress", "boss-progress");
      progress.max = battle.required_hits;
      progress.value = battle.hits;
      content.append(progress);
    }
    const phaseNames = { join: "Набор участников", attack: "Атака", block: "Защита",
      resolving: "Итоги раунда" };
    const state = element("div", "data-grid");
    state.append(
      dataCell("Фаза", phaseNames[battle.phase] || battle.phase),
      dataCell("Участников", battle.participants_count),
      dataCell("В строю", battle.alive_count)
    );
    if (battle.round > 0) {
      state.append(dataCell("Раунд", battle.round),
        dataCell("Урон боссу", `${battle.hits} / ${battle.required_hits}`));
    }
    if (Number.isFinite(battle.deadline_at)) {
      const clock = data._clock || { serverNow: Date.now(), observedAt: performance.now() };
      const node = element("span", "value");
      const cell = element("div", "data-cell");
      cell.append(element("span", "label", "До конца фазы"), node);
      state.append(cell);
      bossCountdown = {
        battleId: battle.battle_id, round: battle.round, phase: battle.phase,
        deadlineAt: battle.deadline_at, node,
        serverNow: clock.serverNow, observedAt: clock.observedAt,
      };
    }
    content.append(state);
    if (Array.isArray(data.available_actions) && data.available_actions.length) {
      addHeading(content, battle.phase === "join" ? "Участие" :
        battle.phase === "attack" ? "Выбрать атаку" : "Выбрать защиту");
      const actions = element("div", "boss-action");
      for (const action of data.available_actions) {
        const button = element("button", "small-button", action.label);
        button.type = "button";
        button.disabled = bossActionInFlight;
        if (data.viewer?.selected_action === action.id) {
          button.classList.add("is-selected");
          button.setAttribute("aria-pressed", "true");
        }
        button.addEventListener("click", () => submitBossAction(action.id));
        actions.append(button);
      }
      content.append(actions);
    }
    addHeading(content, "Моё участие");
    const viewer = data.viewer || {};
    let viewerText;
    if (!viewer.in_battle) {
      viewerText = battle.phase === "join" ?
        "Вы ещё не участвуете. Вступите через кнопку выше или в Telegram." :
        "Вы не участвуете в этой битве.";
    } else if (!viewer.alive) {
      viewerText = "Вы выбыли из битвы.";
    } else if (battle.phase === "join") {
      viewerText = "Вы в числе участников. Ожидайте начала боя.";
    } else if (battle.phase === "resolving") {
      viewerText = "Раунд разрешается. Следите за битвой в Telegram.";
    } else if (viewer.choice_submitted) {
      viewerText = "Ваш выбор принят. До конца фазы его можно изменить здесь или в Telegram.";
    } else {
      viewerText = "Сделайте выбор выше или в Telegram.";
    }
    content.append(notice(viewerText));
    if (Array.isArray(battle.participants) && battle.participants.length) {
      addHeading(content, "Участники");
      const team = element("ul", "boss-team");
      for (const participant of battle.participants) {
        const row = element("li");
        const stateText = participant.alive ?
          `В строю · попаданий: ${participant.hits}` : "Выбыл";
        row.append(element("span", "boss-team-name", participant.title),
          element("span", "boss-team-state", stateText));
        team.append(row);
      }
      content.append(team);
    }
    body.replaceChildren(content);
    updateBossCountdown();
  }

  async function submitBossAction(actionId) {
    const battle = bossSnapshot?.battle;
    if (!sessionToken || currentView !== "boss" || bossActionInFlight || !battle) return;
    const permitted = bossSnapshot.available_actions?.some(action => action.id === actionId);
    if (!permitted) return;
    bossActionInFlight = true;
    for (const button of document.querySelectorAll(".boss-action button")) button.disabled = true;
    setViewStatus("boss", "Отправляем выбор…", false, "action");
    const join = battle.phase === "join";
    const body = { battle_id: battle.battle_id, round: battle.round };
    if (!join) {
      body.phase = battle.phase;
      body.action_id = actionId;
    }
    try {
      await apiRequest(join ? "/api/v1/boss/join" : "/api/v1/boss/action",
        { method: "POST", body });
      clearViewStatus("boss", "action");
      await loadView("boss", true);
    } catch (error) {
      if (error.status === 401) showUnavailable(error.message);
      else {
        await loadView("boss", true);
        if (sessionToken && !["stale_battle", "stale_phase", "stale_round",
            "no_active_battle", "recruitment_closed", "already_acted"].includes(error.code)) {
          setViewStatus("boss", error.message || "Не удалось выполнить действие.", true, "action");
        } else clearViewStatus("boss", "action");
      }
    } finally {
      bossActionInFlight = false;
      if (sessionToken && currentView === "boss" && bossSnapshot) renderBoss(bossSnapshot);
    }
  }

  function updateBossCountdown() {
    const turn = bossCountdown;
    if (!turn || currentView !== "boss") return;
    const remaining = Math.max(0, turn.deadlineAt -
      (turn.serverNow + performance.now() - turn.observedAt));
    turn.node.textContent = `${Math.ceil(remaining / 1000)} с`;
    if (remaining > 0) return;
    for (const button of document.querySelectorAll(".boss-action button")) button.disabled = true;
    const key = `${turn.battleId}:${turn.round}:${turn.phase}:${turn.deadlineAt}`;
    if (bossExpiredKey !== key && sessionToken && !document.hidden) {
      bossExpiredKey = key;
      window.setTimeout(() => {
        if (bossCountdown === turn && currentView === "boss" && !document.hidden) {
          loadView("boss", true);
        }
      }, 0);
    }
  }

  function renderOpponents(data) {
    opponentsSnapshot = data;
    if (inspectedPlayerId !== null && inspectedPlayerSource === "opponents") return;
    const body = document.getElementById("opponents-content");
    const content = element("div");
    if (data.ineligibility === "no_dick") {
      content.append(notice("Сегодня дуэли недоступны: у вашего гнома нет хуя.", true));
    } else if (data.ineligibility) {
      content.append(notice("Дуэль сейчас недоступна; осмотр игроков доступен.", true));
    }
    if (!Array.isArray(data.opponents) || data.opponents.length === 0) {
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
        const inspect = element("button", "small-button", "Осмотреть");
        inspect.type = "button";
        inspect.addEventListener("click", () => inspectOpponent(opponent.user_id));
        const action = element("button", "small-button", "Вызвать");
        action.type = "button";
        const blocker = data.ineligibility || opponent.duel_ineligibility;
        action.disabled = challengeInFlight || Boolean(blocker);
        if (blocker) action.title = blocker === "no_dick" ? "Сегодня без хуя" : "Дуэль сейчас недоступна";
        action.addEventListener("click", () => challengeOpponent(opponent.user_id));
        const actions = element("div", "opponent-actions");
        actions.append(inspect, action);
        if (blocker) actions.append(element("small", "duel-blocker", action.title));
        row.append(identity, actions);
        list.append(row);
      }
      content.append(head, list);
    }
    body.replaceChildren(content);
  }

  async function challengeOpponent(opponentUserId) {
    if (!sessionToken || challengeInFlight ||
        (inspectedPlayerId !== null && inspectedPlayerSource === "opponents")) return;
    challengeInFlight = true;
    for (const button of document.querySelectorAll(".opponent-list button")) button.disabled = true;
    setViewStatus("opponents", "Начинаем дуэль…", false, "action");
    try {
      await apiRequest(START_DUEL_PATH, {
        method: "POST", body: { opponent_user_id: opponentUserId },
      });
      clearViewStatus("opponents", "action");
      await navigate("duel");
    } catch (error) {
      if (error.status === 401) {
        showUnavailable(error.message);
      } else {
        await Promise.all([loadView("opponents", true), loadView("duel", true)]);
        if (sessionToken) setViewStatus("opponents", error.message || "Не удалось начать дуэль. Обновите данные.", true, "action");
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
    setViewStatus("duel", "Передаём выбор…", false, "action");
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
      setViewStatus("duel", "Не удалось проверить состояние дуэли. Обновите экран перед новым ходом.", true, "action");
    } else if (accepted) {
      clearViewStatus("duel", "action");
    } else {
      setViewStatus("duel", failure || "Проверьте состояние дуэли перед повторным выбором.", true, "action");
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
    if (view === "boss") bossPollStartedAt = performance.now();
    const request = (async () => {
      if (!silent) setViewStatus(view, "Загрузка данных…");
      try {
        const data = await apiRequest(API_PATHS[view]);
        if (!sessionToken) return false;
        if (view === "home") renderHome(data);
        else if (view === "opponents") renderOpponents(data);
        else if (view === "hall") renderHall(data);
        else if (view === "boss") renderBoss(data);
        else renderDuel(data);
        clearViewStatus(view, "read");
        return true;
      } catch (error) {
        if (error.status === 401) showUnavailable(error.message);
        else if (sessionToken) setViewStatus(view, error.message || "Не удалось загрузить данные.", true);
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
    if (view !== "boss") bossCountdown = null;
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
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) return;
    if (currentView === "boss") {
      if (loading.boss) {
        loading.boss.then(() => {
          if (!document.hidden && currentView === "boss") loadView("boss", true);
        });
      } else loadView("boss", true);
      return;
    }
    if (currentView !== "duel") return;
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
  window.setInterval(() => { updateCountdown(); updateBossCountdown(); }, COUNTDOWN_TICK_MS);
  window.setInterval(() => {
    const delay = !bossSnapshot?.battle ? BOSS_IDLE_POLL_MS :
      bossSnapshot.battle.phase === "resolving" ? BOSS_WAIT_POLL_MS : BOSS_POLL_MS;
    if (!document.hidden && currentView === "boss" && sessionToken &&
        performance.now() - bossPollStartedAt >= delay) loadView("boss", true);
  }, BOSS_POLL_MS);

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
      await loadView("home");
    } catch {
      showUnavailable("Ссылка уже использована или устарела. Вернитесь в чат и вызовите /duel_app ещё раз.");
    }
  }

  bootstrap();
})();
