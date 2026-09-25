import asyncio
import logging
import random
from html import escape

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest, Forbidden
from telegram.ext import ContextTypes
from text_resources import get_text
from config import (
    TOP_SORT_BY,
    ADMIN_IDS,
    WINNER_100_PTS_GIF,
    MAX_DAILY_POINTS,
    BERSERK_CHANCE,
    DUEL_POST_MESSAGE_CHANCE,
    DUEL_ITEM_STEAL_CHANCE,
    DUEL_ITEM_DROP_CHANCE,
    BOSS_ITEM_DROP_CHANCE,
)
from database import (
    get_or_create_duel_user,
    get_duel_user_by_username,
    get_duel_user_by_id,
    delete_duel_user_by_username,
    apply_duel_result_plan,
    apply_duel_berserk,
    get_duel_top,
    format_user_title,
    format_user_title_plain,
    get_dick_steal_chance,
    get_all_chats,
    reward_boss_victory,
    increment_monthly_bosses_killed,
    get_bosses_defeated,
    is_boss_enabled,
    set_boss_enabled,
    add_duel_inventory_item,
    get_duel_inventory,
    has_huecrab,
    create_duel_item_event_from_inventory,
    restore_unpublished_duel_drop,
    set_duel_item_event_message,
    transfer_duel_inventory_item,
)
from handlers.duel_text import (
    _boss_alive_players,
    _boss_all_alive_chosen,
    _boss_battle_hero,
    _boss_phase_status,
    _legacy_plural_rounds,
    _legacy_plural_rounds_early,
    _plural_rounds,
    get_huyanie_title,
    get_win_title,
    get_loss_title,
    get_stolen_dicks_title,
    ATTACK_PHRASES,
    BLOCK_PHRASES,
    HIT_PHRASES,
    MISS_PHRASES,
    SUICIDE_PHRASES,
    TARGET_NAMES,
    _build_duel_block_text,
    _build_duel_miss_text,
    _build_berserk_text,
    get_round_flavor_text,
)
from handlers.duel_formatting import (
    _boss_phase_text,
    _boss_players_status_text,
    boss_player_title as _boss_player_title,
)
from handlers.boss_presentation import (
    BOSS_REQUIRED_HITS,
    BOSS_ZONE_NAMES,
    _boss_death_epitaph,
    _boss_final_report,
    _boss_survivor_epitaph,
)
from handlers.boss_state import (
    _apply_boss_round_result,
    _begin_boss_round,
    _enter_boss_block_phase,
    _record_boss_attack_choice,
    _record_boss_block_choice,
)
from handlers.duel_input import extract_username as _extract_username
from handlers.duel_service import list_duel_opponents
from handlers.player_stats import player_stats_read_model, format_player_stats_telegram
from handlers.duel_service import (
    start_persistent_duel, submit_persistent_duel_attack,
    submit_persistent_duel_block,
)
from handlers.persistent_duel_publisher import recover_persistent_duel_chat
from handlers.duel_state import (
    _advance_duel_round,
    _build_duel_result_plan,
    _get_duel_participant_ineligibility,
    DUEL_MOVE_TIMEOUT_SECONDS,
    _is_miss_roll,
    _is_berserk_roll,
    _is_duel_item_steal_roll,
    choose_duel_item_to_steal,
    _is_duel_post_message_roll,
    _is_suicide_roll,
    _resolve_zone_outcome,
    _set_attack_choice,
    resolve_duel_round,
)
from handlers.duel_catalog import (
    DWARFS_FACTS,
    DUEL_POST_MESSAGES,
    _DUEL_POST_MESSAGES_PATH,
    _load_duel_post_messages,
)
from handlers.duel_messaging import (
    AUTO_DELETE_DELAY,
    delete_messages_job,
    schedule_auto_delete,
    send_and_schedule,
)
from handlers.hyperborean_event import (
    ACTIVE_HYPERBOREAN_EVENTS,
    hyperboreic_huy_callback,
    hyperboreic_huy_daily_job,
)
from handlers.boss_registration import (
    _boss_clear_registrations,
    _boss_get_registered_chat_ids,
    _boss_get_registered_users,
    _boss_register_user,
    _boss_registration_is_open,
)
from handlers.duel_items import (
    DUEL_ITEM_EVENT_CALLBACK_PREFIX,
    DUEL_ITEMS,
    format_duel_display_inventory,
    get_droppable_duel_inventory,
    get_duel_item_name,
)

MOVE_TIMEOUT = DUEL_MOVE_TIMEOUT_SECONDS  # 10 секунд на ход

async def gnomed_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if not message or not update.effective_chat:
        return

    target_message_id = (
        message.reply_to_message.message_id
        if message.reply_to_message is not None
        else None
    )
    if target_message_id is None:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=get_text("duel.gnomed.reply_required"),
        )
    elif not DUEL_POST_MESSAGES:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=get_text("duel.gnomed.catalog_unavailable"),
        )
    else:
        phrase = random.choice(DUEL_POST_MESSAGES)
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=get_text("duel.gnomed.response", phrase=phrase),
            reply_to_message_id=target_message_id,
        )

    try:
        await message.delete()
    except Exception:
        pass


def _maybe_drop_loser_inventory_item(chat_id: int, loser_id: int) -> dict | None:
    inventory = get_droppable_duel_inventory(
        get_duel_inventory(chat_id, loser_id)
    )
    if not inventory:
        return None
    if random.random() >= DUEL_ITEM_DROP_CHANCE:
        return None

    instance = random.choice(inventory)
    return create_duel_item_event_from_inventory(chat_id, loser_id, instance["id"])


async def _publish_duel_drop(context, drop: dict) -> None:
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton(
        get_text("duel.item_event.button"),
        callback_data=f"{DUEL_ITEM_EVENT_CALLBACK_PREFIX}{drop['event_id']}",
    )]])
    message = None
    try:
        text = get_text(
            "duel.finish.item_drop",
            item_name=escape(get_duel_item_name(drop["item_id"])),
        )
        message = await context.bot.send_message(
            chat_id=drop["chat_id"],
            text=text,
            parse_mode="HTML",
            reply_markup=keyboard,
        )
        if not set_duel_item_event_message(drop["event_id"], message.message_id, text):
            raise RuntimeError("Could not bind duel drop to pickup message")
    except Exception:
        logging.exception("Не удалось опубликовать выпавший предмет в чате %s", drop["chat_id"])
        restored = False
        try:
            restored = restore_unpublished_duel_drop(
                drop, message.message_id if message is not None else None,
            )
            if not restored:
                logging.error("Не удалось вернуть неопубликованный предмет %s", drop["event_id"])
        except Exception:
            logging.exception("Не удалось откатить выпадение предмета %s", drop["event_id"])
        if restored and message is not None:
            try:
                await context.bot.delete_message(drop["chat_id"], message.message_id)
            except Exception:
                logging.exception("Не удалось удалить недоступную кнопку предмета %s", drop["event_id"])


def _maybe_steal_loser_inventory_item(
    chat_id: int,
    winner_id: int,
    loser_id: int,
) -> str | None:
    inventory = get_droppable_duel_inventory(
        get_duel_inventory(chat_id, loser_id)
    )
    instance = choose_duel_item_to_steal(inventory, random)
    if instance is None:
        return None
    if not transfer_duel_inventory_item(
        chat_id,
        loser_id,
        winner_id,
        instance["id"],
    ):
        return None
    return get_duel_item_name(instance["item_id"])


def _maybe_award_boss_item(chat_id: int, battle: dict) -> str | None:
    survivors = _boss_alive_players(battle)
    if not survivors:
        return None
    if random.random() >= BOSS_ITEM_DROP_CHANCE:
        return None

    survivor = random.choice(survivors)
    item = random.choice(DUEL_ITEMS)
    add_duel_inventory_item(chat_id, survivor["tg_user"].id, item["id"])
    return get_text(
        "boss.report.item_loot",
        item_name=escape(item["name"]),
        survivor=_boss_player_title(survivor),
    )


# ============================================================
# АКТИВНЫЕ ДУЭЛИ
# ============================================================

# Хранилище активных дуэлей в памяти:
# ACTIVE_DUELS[chat_id] = duel_state
#
# В одном чате одновременно может идти только одна дуэль.
# Legacy-only compatibility for direct unit tests below. bot.py registers the
# persistent adapter; no production ordinary-duel handler reads this mapping.
ACTIVE_DUELS = {}

# ============================================================
# 🍆 ГИПЕРБОРЕЙСКИЙ ХУЙ
# ============================================================



# Проверяем независимо от игровых событий раз в 15 минут.
# Это не дневной лимит: после полуночи вероятность не обнуляется.


# Одно активное событие на чат.
# Состояние живёт в памяти и не имеет ежедневного сброса.



# ============================================================
# 👹 АКТИВНЫЕ БИТВЫ С БОССАМИ
# ============================================================

# В одном чате одновременно может идти только одна битва с боссом.
#
# ACTIVE_BOSS_BATTLES[chat_id] = {
#     "boss": {...},
#     "participants": {
#         user_id: {
#             "tg_user": ...,
#             "data": {...},
#             "attack": None,
#             "block": None,
#             "alive": True,
#         }
#     },
#     "hits": 0,
#     "round": 0,
#     "phase": "join" / "battle",
#     "message_id": None,
#     "task": None,
#     "lock": asyncio.Lock(),
# }


BOSSES = [
    {
        "name": get_text("boss.catalog.deep_snouted_baron.name"),
        "emoji": get_text("boss.catalog.deep_snouted_baron.emoji"),
        "description": get_text(
            "boss.catalog.deep_snouted_baron.description"
        ),
    },
    {
        "name": get_text("boss.catalog.dick_crusher_face_eater.name"),
        "emoji": get_text("boss.catalog.dick_crusher_face_eater.emoji"),
        "description": get_text(
            "boss.catalog.dick_crusher_face_eater.description"
        ),
    },
    {
        "name": get_text("boss.catalog.prince_of_underground_chaos.name"),
        "emoji": get_text("boss.catalog.prince_of_underground_chaos.emoji"),
        "description": get_text(
            "boss.catalog.prince_of_underground_chaos.description"
        ),
    },
    {
        "name": get_text("boss.catalog.great_knife_beard.name"),
        "emoji": get_text("boss.catalog.great_knife_beard.emoji"),
        "description": get_text(
            "boss.catalog.great_knife_beard.description"
        ),
    },
    {
        "name": get_text("boss.catalog.dick_devourer.name"),
        "emoji": get_text("boss.catalog.dick_devourer.emoji"),
        "description": get_text("boss.catalog.dick_devourer.description"),
    },
    {
        "name": get_text("boss.catalog.chizyanovsky_skier.name"),
        "emoji": get_text("boss.catalog.chizyanovsky_skier.emoji"),
        "description": get_text("boss.catalog.chizyanovsky_skier.description"),
    },
]


# ============================================================
# СЛОВАРИ ДЛЯ КНОПОК И ТЕКСТА
# ============================================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ============================================================

# ============================================================
# КЛАВИАТУРЫ
# ============================================================

def _get_strike_keyboard(turn_id: int, duel_id: int | None = None) -> InlineKeyboardMarkup:
    """
    Кнопки атаки привязаны к конкретному turn_id.

    Благодаря этому старая клавиатура от предыдущего хода
    не сможет выполнить действие в новом ходе.
    """

    buttons = [
        [
            InlineKeyboardButton(
                get_text("duel.live.keyboards.strike.head"),
                callback_data=f"duel_strike_head_{duel_id}_{turn_id}" if duel_id is not None else f"duel_strike_head_{turn_id}",
            ),
            InlineKeyboardButton(
                get_text("duel.live.keyboards.strike.body"),
                callback_data=f"duel_strike_body_{duel_id}_{turn_id}" if duel_id is not None else f"duel_strike_body_{turn_id}",
            ),
            InlineKeyboardButton(
                get_text("duel.live.keyboards.strike.dick"),
                callback_data=f"duel_strike_dick_{duel_id}_{turn_id}" if duel_id is not None else f"duel_strike_dick_{turn_id}",
            ),
        ]
    ]

    return InlineKeyboardMarkup(buttons)


def _get_block_keyboard(turn_id: int, duel_id: int | None = None) -> InlineKeyboardMarkup:
    """
    Кнопки защиты привязаны к конкретному turn_id.
    """

    buttons = [
        [
            InlineKeyboardButton(
                get_text("duel.live.keyboards.block.head"),
                callback_data=f"duel_block_head_{duel_id}_{turn_id}" if duel_id is not None else f"duel_block_head_{turn_id}",
            ),
            InlineKeyboardButton(
                get_text("duel.live.keyboards.block.body"),
                callback_data=f"duel_block_body_{duel_id}_{turn_id}" if duel_id is not None else f"duel_block_body_{turn_id}",
            ),
            InlineKeyboardButton(
                get_text("duel.live.keyboards.block.dick"),
                callback_data=f"duel_block_dick_{duel_id}_{turn_id}" if duel_id is not None else f"duel_block_dick_{turn_id}",
            ),
        ]
    ]

    return InlineKeyboardMarkup(buttons)


# ============================================================
# НАЧАЛО ИНТЕРАКТИВНОГО БОЯ
# ============================================================

async def _start_interactive_fight(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    attacker_tg,
    defender_tg,
    attacker_data: dict,
    defender_data: dict,
    original_msg_id: int = None,
):

    duel_state = {
        "attacker_tg": attacker_tg,
        "defender_tg": defender_tg,

        "attacker_data": attacker_data,
        "defender_data": defender_data,

        # Текущая фаза:
        # attack = атакующий выбирает атаку
        # block = защищающийся выбирает блок
        "phase": "attack",

        "attack_zone": None,

        "round": 1,

        # Уникальный номер текущего хода.
        # Меняется при каждом переходе к следующему ходу.
        "turn_id": 1,

        "message_id": None,

        "turn_task": None,

        # Защита от двух одновременных callback.
        "lock": asyncio.Lock(),

        "original_msg_id": original_msg_id,
    }

    ACTIVE_DUELS[chat_id] = duel_state

    att_title = format_user_title(attacker_data)
    def_title = format_user_title(defender_data)

    text = get_text(
        "duel.live.start",
        attacker_title=att_title,
        defender_title=def_title,
        move_timeout=MOVE_TIMEOUT,
    )

    bot_msg = await context.bot.send_message(
        chat_id=chat_id,
        text=text,
        parse_mode="HTML",
        reply_markup=_get_strike_keyboard(
            duel_state["turn_id"]
        ),
    )

    duel_state["message_id"] = bot_msg.message_id

    task = asyncio.create_task(
        _auto_move_timer(
            context,
            chat_id,
            duel_state["round"],
            phase="attack",
            turn_id=duel_state["turn_id"],
        )
    )

    duel_state["turn_task"] = task


# ============================================================
# АВТОМАТИЧЕСКИЙ ХОД ПО ТАЙМАУТУ
# ============================================================

async def _auto_move_timer(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    round_num: int,
    phase: str,
    turn_id: int,
):

    await asyncio.sleep(MOVE_TIMEOUT)

    duel = ACTIVE_DUELS.get(chat_id)

    if not duel:
        return

    async with duel["lock"]:

        # Проверяем абсолютно все параметры текущего хода.
        #
        # Это важно: старый таймер не должен вмешаться
        # в новый раунд или новый ход.
        if (
            duel.get("round") != round_num
            or duel.get("phase") != phase
            or duel.get("turn_id") != turn_id
        ):
            return

        random_choice = random.choice(
            ["head", "body", "dick"]
        )

        if phase == "attack":

            att_title = format_user_title(
                duel["attacker_data"]
            )

            try:
                timeout_msg = await context.bot.send_message(
                    chat_id=chat_id,
                    text=get_text("duel.live.timeout.attack", title=att_title),
                    parse_mode="HTML",
                )
                schedule_auto_delete(
                    context,
                    chat_id,
                    [timeout_msg.message_id],
                )
            except Exception:
                pass

            await _process_attack_choice(
                context,
                chat_id,
                random_choice,
            )

        elif phase == "block":

            def_title = format_user_title(
                duel["defender_data"]
            )

            try:
                timeout_msg = await context.bot.send_message(
                    chat_id=chat_id,
                    text=get_text("duel.live.timeout.block", title=def_title),
                    parse_mode="HTML",
                )
                schedule_auto_delete(
                    context,
                    chat_id,
                    [timeout_msg.message_id],
                )
            except Exception:
                pass

            await _process_block_choice(
                context,
                chat_id,
                random_choice,
            )


# ============================================================
# CALLBACK-КНОПКИ АТАКИ / ЗАЩИТЫ
# ============================================================

async def duel_strike_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    query = update.callback_query

    if not query or not query.data:
        return

    chat_id = update.effective_chat.id
    duel = ACTIVE_DUELS.get(chat_id)

    if not duel:
        await query.answer(
            get_text("duel.action_alert.no_active_duel"),
            show_alert=True,
        )
        return

    # ВАЖНО:
    # Все проверки и изменение состояния находятся
    # внутри одного lock.
    #
    # Если пользователь очень быстро нажмет две кнопки,
    # второй callback дождется первого и увидит уже
    # измененную фазу.
    async with duel["lock"]:

        callback_data = query.data
        user_id = query.from_user.id

        # ====================================================
        # АТАКА
        # ====================================================

        if callback_data.startswith("duel_strike_"):

            # Сейчас не фаза атаки.
            if duel["phase"] != "attack":
                await query.answer(
                    get_text("duel.action_alert.strike.wrong_phase"),
                    show_alert=True,
                )
                return

            # Только атакующий может выбирать атаку.
            if user_id != duel["attacker_tg"].id:
                await query.answer(
                    get_text("duel.action_alert.strike.wrong_actor"),
                    show_alert=True,
                )
                return

            # Ожидаем:
            # duel_strike_head_1
            # duel_strike_body_1
            # duel_strike_dick_1

            parts = callback_data.split("_")

            if len(parts) != 4:
                await query.answer(
                    get_text("duel.action_alert.stale_button"),
                    show_alert=True,
                )
                return

            strike_zone = parts[2]

            try:
                button_turn_id = int(parts[3])
            except ValueError:
                await query.answer(
                    get_text("duel.action_alert.stale_button"),
                    show_alert=True,
                )
                return

            # Кнопка должна принадлежать именно текущему ходу.
            if button_turn_id != duel["turn_id"]:
                await query.answer(
                    get_text("duel.action_alert.turn_ended"),
                    show_alert=True,
                )
                return

            if strike_zone not in TARGET_NAMES:
                await query.answer(
                    get_text("duel.action_alert.strike.unknown_zone"),
                    show_alert=True,
                )
                return

            # Все проверки прошли.
            # Теперь отменяем таймер.
            if (
                duel.get("turn_task")
                and not duel["turn_task"].done()
            ):
                duel["turn_task"].cancel()

            await query.answer()

            await _process_attack_choice(
                context,
                chat_id,
                strike_zone,
            )

            return

        # ====================================================
        # ЗАЩИТА
        # ====================================================

        if callback_data.startswith("duel_block_"):

            # Сейчас не фаза защиты.
            if duel["phase"] != "block":
                await query.answer(
                    get_text("duel.action_alert.block.wrong_phase"),
                    show_alert=True,
                )
                return

            # Только защищающийся может выбирать защиту.
            if user_id != duel["defender_tg"].id:
                await query.answer(
                    get_text("duel.action_alert.block.wrong_actor"),
                    show_alert=True,
                )
                return

            # Ожидаем:
            # duel_block_head_2
            # duel_block_body_2
            # duel_block_dick_2

            parts = callback_data.split("_")

            if len(parts) != 4:
                await query.answer(
                    get_text("duel.action_alert.stale_button"),
                    show_alert=True,
                )
                return

            block_zone = parts[2]

            try:
                button_turn_id = int(parts[3])
            except ValueError:
                await query.answer(
                    get_text("duel.action_alert.stale_button"),
                    show_alert=True,
                )
                return

            # Кнопка должна принадлежать текущему ходу.
            if button_turn_id != duel["turn_id"]:
                await query.answer(
                    get_text("duel.action_alert.turn_ended"),
                    show_alert=True,
                )
                return

            if block_zone not in TARGET_NAMES:
                await query.answer(
                    get_text("duel.action_alert.block.unknown_zone"),
                    show_alert=True,
                )
                return

            if (
                duel.get("turn_task")
                and not duel["turn_task"].done()
            ):
                duel["turn_task"].cancel()

            await query.answer()

            await _process_block_choice(
                context,
                chat_id,
                block_zone,
            )

            return


# Алиас для совместимости с bot.py
duel_action_callback = duel_strike_callback


# ============================================================
# ОБРАБОТКА АТАКИ
# ============================================================

async def _process_attack_choice(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    strike_zone: str,
):

    duel = ACTIVE_DUELS.get(chat_id)

    if not duel:
        return


    # Теперь ход принадлежит защищающемуся.

    # Новый turn_id = новая клавиатура.
    _set_attack_choice(duel, strike_zone)

    att_title = format_user_title(
        duel["attacker_data"]
    )

    def_title = format_user_title(
        duel["defender_data"]
    )

    text = get_text(
        "duel.live.defense_transition",
        round=duel["round"],
        attacker_title=att_title,
        defender_title=def_title,
        move_timeout=MOVE_TIMEOUT,
    )

    try:
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=duel["message_id"],
            text=text,
            parse_mode="HTML",
            reply_markup=_get_block_keyboard(
                duel["turn_id"]
            ),
        )

    except Exception:

        bot_msg = await context.bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode="HTML",
            reply_markup=_get_block_keyboard(
                duel["turn_id"]
            ),
        )

        duel["message_id"] = bot_msg.message_id

    task = asyncio.create_task(
        _auto_move_timer(
            context,
            chat_id,
            duel["round"],
            phase="block",
            turn_id=duel["turn_id"],
        )
    )

    duel["turn_task"] = task


# ============================================================
# ОБРАБОТКА ЗАЩИТЫ
# ============================================================

async def _process_block_choice(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    block_zone: str,
):

    duel = ACTIVE_DUELS.get(chat_id)

    if not duel:
        return

    strike_zone = duel["attack_zone"]

    attacker_data = duel["attacker_data"]
    defender_data = duel["defender_data"]

    att_title = format_user_title(attacker_data)
    def_title = format_user_title(defender_data)
    resolution = resolve_duel_round(strike_zone, block_zone, random)

    # ========================================================
    # 1. Шанс 1% — самоубийство атаковавшего
    # ========================================================

    if resolution.outcome == "suicide":

        res_text = get_text(
            "duel.live.outcomes.suicide",
            attacker_title=att_title,
            suicide_phrase=resolution.outcome_phrase,
            defender_title=def_title,
        )

        await _finish_duel(
            context,
            chat_id,
            winner=defender_data,
            loser=attacker_data,
            custom_text=res_text,
            strike_zone=strike_zone,
            block_zone=block_zone,
        )

        return

    # ========================================================
    # 2. Шанс 5% — промах
    # ========================================================

    if resolution.outcome == "miss":

        # Смена ролей.
        _advance_duel_round(duel)

        # Новый ход = новая кнопка.
        new_att_title = format_user_title(
            duel["attacker_data"]
        )

        new_def_title = format_user_title(
            duel["defender_data"]
        )

        text = _build_duel_miss_text(
            att_title,
            resolution.attack_phrase,
            strike_zone,
            resolution.outcome_phrase,
            new_att_title,
            new_def_title,
            MOVE_TIMEOUT,
        )

        try:
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=duel["message_id"],
                text=text,
                parse_mode="HTML",
                reply_markup=_get_strike_keyboard(
                    duel["turn_id"]
                ),
            )

        except Exception:

            bot_msg = await context.bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode="HTML",
                reply_markup=_get_strike_keyboard(
                    duel["turn_id"]
                ),
            )

            duel["message_id"] = bot_msg.message_id

        task = asyncio.create_task(
            _auto_move_timer(
                context,
                chat_id,
                duel["round"],
                phase="attack",
                turn_id=duel["turn_id"],
            )
        )

        duel["turn_task"] = task

        return

    # ========================================================
    # 3. Сравнение УДАРА и БЛОКА
    # ========================================================

    if resolution.outcome == "block":

        # Смена ролей.
        _advance_duel_round(duel)

        # Новый ход = новая кнопка.
        new_att_title = format_user_title(
            duel["attacker_data"]
        )

        new_def_title = format_user_title(
            duel["defender_data"]
        )

        text = _build_duel_block_text(
            att_title,
            def_title,
            resolution.attack_phrase,
            strike_zone,
            resolution.outcome_phrase,
            new_att_title,
            new_def_title,
            MOVE_TIMEOUT,
        )

        try:
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=duel["message_id"],
                text=text,
                parse_mode="HTML",
                reply_markup=_get_strike_keyboard(
                    duel["turn_id"]
                ),
            )

        except Exception:

            bot_msg = await context.bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode="HTML",
                reply_markup=_get_strike_keyboard(
                    duel["turn_id"]
                ),
            )

            duel["message_id"] = bot_msg.message_id

        task = asyncio.create_task(
            _auto_move_timer(
                context,
                chat_id,
                duel["round"],
                phase="attack",
                turn_id=duel["turn_id"],
            )
        )

        duel["turn_task"] = task

    # ========================================================
    # 4. Точный удар
    # ========================================================

    else:

        res_text = get_text(
            "duel.live.outcomes.hit",
            attacker_title=att_title,
            attack_phrase=resolution.attack_phrase,
            strike_target=TARGET_NAMES[strike_zone],
            defender_title=def_title,
            block_target=TARGET_NAMES[block_zone],
            hit_phrase=resolution.outcome_phrase,
        )

        await _finish_duel(
            context,
            chat_id,
            winner=attacker_data,
            loser=defender_data,
            custom_text=res_text,
            strike_zone=strike_zone,
            block_zone=block_zone,
        )


# ============================================================
# ЗАВЕРШЕНИЕ ДУЭЛИ
# ============================================================

async def _finish_duel(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    winner: dict,
    loser: dict,
    custom_text: str,
    strike_zone: str = None,
    block_zone: str = None,
):

    duel = ACTIVE_DUELS.pop(chat_id, None)
    rounds_count = duel.get("round", 1) if duel else 1

    # Шанс кражи не зависит от раундов: база +1% за каждую победу
    # проигравшего за сегодня (накапливается, когда он побеждал).
    is_dick_stolen = (
        loser["points"] == 0
        or random.random() < get_dick_steal_chance(loser.get("daily_wins", 0))
    )

    try:

        win_title = format_user_title(winner)
        result_plan = _build_duel_result_plan(
            winner,
            loser,
            is_dick_stolen,
            format_user_title_plain(winner, include_dwarf_name=False),
            MAX_DAILY_POINTS,
        )
        w_after, l_after = apply_duel_result_plan(
            chat_id,
            result_plan,
        )

    except Exception:

        bot_msg = await context.bot.send_message(
            chat_id,
            get_text("duel.finish.error"),
        )

        schedule_auto_delete(
            context,
            chat_id,
            [bot_msg.message_id],
        )

        return

    stolen_item_name = None
    if is_dick_stolen:
        try:
            stolen_item_name = _maybe_steal_loser_inventory_item(
                chat_id,
                winner["user_id"],
                loser["user_id"],
            )
        except Exception:
            logging.exception("Ошибка кражи предмета после дуэли в чате %s", chat_id)

    lose_title = format_user_title(loser)

    res_msg = get_text(
        "duel.finish.result",
        custom_text=custom_text,
        winner_title=win_title,
        loser_title=lose_title,
        winner_points=w_after,
        loser_points=l_after,
    )

    stats_text = get_text(
        "duel.finish.stats",
        rounds_count=rounds_count,
        rounds_label=_plural_rounds(rounds_count),
        round_flavor=get_round_flavor_text(rounds_count),
    )

    if is_dick_stolen:

        if stolen_item_name is not None:
            res_msg += get_text(
                "duel.finish.item_stolen",
                item_name=escape(stolen_item_name),
            )

        fact = random.choice(
            DWARFS_FACTS
        )

        res_msg += get_text(
            "duel.finish.stolen",
            loser_title=lose_title,
            stats_text=stats_text,
            fact=fact,
        )
    else:
        res_msg += stats_text

    if _is_berserk_roll(random.random()):
        berserker = random.choice((winner, loser))
        victim = loser if berserker is winner else winner
        berserker_title = win_title if berserker is winner else lose_title
        victim_title = lose_title if victim is loser else win_title
        try:
            berserk_applied = apply_duel_berserk(
                chat_id,
                berserker["user_id"],
                victim["user_id"],
                format_user_title_plain(berserker, include_dwarf_name=False),
            )
        except Exception:
            logging.exception("Не удалось применить berserk event в чате %s", chat_id)
        else:
            res_msg += _build_berserk_text(
                berserker_title,
                victim_title,
                already_stolen=not berserk_applied,
            )

    if DUEL_POST_MESSAGES and _is_duel_post_message_roll(random.random()):
        res_msg += (
            f"\n\n{get_text('duel.finish.post_message.prefix')}\n"
            f"{escape(random.choice(DUEL_POST_MESSAGES))}"
        )

    if duel and duel.get("message_id"):

        try:
            await context.bot.delete_message(
                chat_id=chat_id,
                message_id=duel["message_id"],
            )
        except Exception:
            pass

    bot_msg = await context.bot.send_message(
        chat_id=chat_id,
        text=res_msg,
        parse_mode="HTML",
    )

    try:
        dropped_item = _maybe_drop_loser_inventory_item(chat_id, loser["user_id"])
    except Exception:
        logging.exception("Ошибка выпадения предмета после дуэли в чате %s", chat_id)
    else:
        if dropped_item is not None:
            await _publish_duel_drop(context, dropped_item)

    to_delete = []

    if not is_dick_stolen:
        to_delete.append(
            bot_msg.message_id
        )

    if duel and duel.get("original_msg_id"):
        to_delete.append(
            duel["original_msg_id"]
        )

    if to_delete:
        schedule_auto_delete(
            context,
            chat_id,
            to_delete,
        )

    reached_max = result_plan["winner_reached_max"]

    if reached_max and WINNER_100_PTS_GIF:

        try:
            await context.bot.send_animation(
                chat_id=chat_id,
                animation=WINNER_100_PTS_GIF,
                caption=get_text(
                    "duel.finish.max_points_caption",
                    winner_title=win_title,
                    max_daily_points=MAX_DAILY_POINTS,
                ),
                parse_mode="HTML",
            )
        except Exception:
            pass


# ============================================================
# ПОИСК И НАЧАЛО ДУЭЛИ
# ============================================================

async def _process_duel_fight(
    context: ContextTypes.DEFAULT_TYPE,
    initiator_tg,
    target_username: str,
    chat_id: int,
    original_msg_id: int = None,
):

    if chat_id in ACTIVE_DUELS:

        bot_msg = await context.bot.send_message(
            chat_id,
            get_text("duel.admission.active_duel"),
        )

        schedule_auto_delete(
            context,
            chat_id,
            [bot_msg.message_id],
        )

        return

    if (
        initiator_tg.username
        and initiator_tg.username.lower()
        == target_username.lower()
    ):

        bot_msg = await context.bot.send_message(
            chat_id,
            get_text("duel.admission.self_target"),
        )

        schedule_auto_delete(
            context,
            chat_id,
            [bot_msg.message_id],
        )

        return

    initiator = get_or_create_duel_user(
        initiator_tg,
        chat_id,
    )

    init_title = format_user_title(
        initiator
    )

    initiator_ineligibility = _get_duel_participant_ineligibility(
        initiator
    )

    if initiator_ineligibility == "no_dick":

        bot_msg = await context.bot.send_message(
            chat_id,
            (
                get_text("duel.admission.participant.no_dick", title=init_title)
            ),
            parse_mode="HTML",
        )

        schedule_auto_delete(
            context,
            chat_id,
            [bot_msg.message_id],
        )

        return

    if initiator_ineligibility == "no_points":

        bot_msg = await context.bot.send_message(
            chat_id,
            get_text("duel.admission.initiator.no_points"),
        )

        schedule_auto_delete(
            context,
            chat_id,
            [bot_msg.message_id],
        )

        return

    opponent = get_duel_user_by_username(
        target_username,
        chat_id,
    )

    if not opponent:

        bot_msg = await context.bot.send_message(
            chat_id,
            (
                get_text("duel.admission.opponent.not_found", username=target_username)
            ),
            parse_mode="HTML",
        )

        schedule_auto_delete(
            context,
            chat_id,
            [bot_msg.message_id],
        )

        return

    if opponent["user_id"] == initiator["user_id"]:

        bot_msg = await context.bot.send_message(
            chat_id,
            get_text("duel.admission.self_target"),
        )

        schedule_auto_delete(
            context,
            chat_id,
            [bot_msg.message_id],
        )

        return

    opp_title = format_user_title(
        opponent
    )

    opponent_ineligibility = _get_duel_participant_ineligibility(
        opponent
    )

    if opponent_ineligibility == "no_dick":

        bot_msg = await context.bot.send_message(
            chat_id,
            (
                get_text("duel.admission.participant.no_dick", title=opp_title)
            ),
            parse_mode="HTML",
        )

        schedule_auto_delete(
            context,
            chat_id,
            [bot_msg.message_id],
        )

        return

    if opponent_ineligibility == "no_points":

        bot_msg = await context.bot.send_message(
            chat_id,
            (
                get_text("duel.admission.opponent.no_points", title=opp_title)
            ),
            parse_mode="HTML",
        )

        schedule_auto_delete(
            context,
            chat_id,
            [bot_msg.message_id],
        )

        return

    class SimpleTGUser:

        def __init__(self, uid, uname):
            self.id = uid
            self.username = uname

    opponent_tg = SimpleTGUser(
        opponent["user_id"],
        opponent["username"],
    )

    # Случайно определяем, кто будет атаковать первым.
    # Инициатор дуэли больше не получает автоматического преимущества.
    if random.choice([True, False]):
        first_attacker_tg = initiator_tg
        first_defender_tg = opponent_tg
        first_attacker_data = initiator
        first_defender_data = opponent
    else:
        first_attacker_tg = opponent_tg
        first_defender_tg = initiator_tg
        first_attacker_data = opponent
        first_defender_data = initiator

    await _start_interactive_fight(
        context=context,
        chat_id=chat_id,
        attacker_tg=first_attacker_tg,
        defender_tg=first_defender_tg,
        attacker_data=first_attacker_data,
        defender_data=first_defender_data,
        original_msg_id=original_msg_id,
    )


# ============================================================
# КОМАНДА /duel
# ============================================================

async def duel_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if (
        not update.message
        or not update.message.from_user
        or not update.message.chat
    ):
        return

    chat_id = update.message.chat_id

    initiator_tg = update.message.from_user

    # --------------------------------------------------------
    # Проверяем наличие хуя ДО выбора соперника.
    # --------------------------------------------------------
    initiator = get_or_create_duel_user(
        initiator_tg,
        chat_id,
    )

    initiator_ineligibility = _get_duel_participant_ineligibility(initiator)
    if initiator_ineligibility == "no_dick":
        await send_and_schedule(
            update,
            context,
            get_text("duel.command.no_dick"),
        )
        return

    target_username = _extract_username(
        update,
        context,
    )

    # --------------------------------------------------------
    # Если соперник не указан — показываем список.
    # --------------------------------------------------------

    if not target_username:

        selection = list_duel_opponents(chat_id, initiator["user_id"])
        if selection.ineligibility == "no_dick":
            await send_and_schedule(
                update,
                context,
                get_text("duel.command.no_dick"),
            )
            return

        if selection.ineligibility == "no_points":
            await send_and_schedule(
                update,
                context,
                get_text("duel.admission.initiator.no_points"),
            )
            return

        keyboard = []

        for opponent in selection.opponents:
            label = get_text("duel.selection.button_label", title=opponent.title)

            keyboard.append(
                [
                    InlineKeyboardButton(
                        label,
                        callback_data=f"start_duel_{opponent.username}",
                    )
                ]
            )

        if not keyboard:

            await send_and_schedule(
                update,
                context,
                (
                    get_text("duel.selection.no_opponents")
                ),
            )

            return

        reply_markup = InlineKeyboardMarkup(
            keyboard
        )

        await send_and_schedule(
            update,
            context,
            get_text("duel.selection.prompt"),
            reply_markup=reply_markup,
        )

        return

    # --------------------------------------------------------
    # Соперник указан напрямую.
    # --------------------------------------------------------

    schedule_auto_delete(
        context,
        chat_id,
        [update.message.message_id],
    )

    await _process_persistent_duel_fight(
        context,
        initiator_tg,
        target_username,
        chat_id,
        original_msg_id=update.message.message_id,
    )


# ============================================================
# CALLBACK ВЫБОРА СОПЕРНИКА
# ============================================================

async def duel_select_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    query = update.callback_query

    if (
        not query
        or not query.data
        or not query.data.startswith("start_duel_")
    ):
        return

    target_username = query.data.replace(
        "start_duel_",
        "",
    )

    initiator_tg = query.from_user
    chat_id = update.effective_chat.id

    await query.answer()

    try:
        await query.message.delete()
    except Exception:
        pass

    await _process_persistent_duel_fight(
        context,
        initiator_tg,
        target_username,
        chat_id,
    )


# ============================================================
# ЗВАНИЯ ЗА ХУЯНИЕ
# ============================================================

HUYANIE_TITLES = {
    10: "🍆 Начинающий Хуянист",
    20: "🍆 Подмастерье Хуяния",
    30: "🍆 Практикующий Хуянист",
    40: "🍆 Опытный Хуянист",
    50: "🍆 Мастер Хуяния",
    60: "🍆 Великий Хуянист",
    70: "🍆 Архимастер Хуяния",
    80: "🍆 Верховный Хуянист",
    90: "🍆 Гроссмейстер Хуяния",
    100: "👑 Великий Магистр Хуяния",
}


def _legacy_get_huyanie_title(stolen_dicks_count: int) -> str:
    count = int(stolen_dicks_count or 0)

    if count < 10:
        return "Нет звания"

    level = min((count // 10) * 10, 100)

    return HUYANIE_TITLES[level]


# ============================================================
# СТАТИСТИКА
# ============================================================

async def duel_stats_command(update, context):
    if not update.message or not update.message.from_user or not update.message.chat:
        return

    chat_id = update.message.chat_id

    get_or_create_duel_user(update.message.from_user, chat_id)
    model = player_stats_read_model(chat_id, update.message.from_user.id)
    if model is not None:
        await send_and_schedule(update, context, format_player_stats_telegram(model))


async def inspect_command(update, context):
    if not update.message or not update.message.from_user or not update.message.chat:
        return
    message = update.message
    chat_id = message.chat_id
    reply_user = getattr(getattr(message, "reply_to_message", None), "from_user", None)
    target = None
    if reply_user is not None:
        target = get_duel_user_by_id(chat_id, reply_user.id, read_only=True)
    else:
        username = _extract_username(update, context)
        if username:
            target = get_duel_user_by_username(username, chat_id, read_only=True)
        else:
            await send_and_schedule(update, context, get_text("duel.inspect.usage"))
            return
    if target is None:
        await send_and_schedule(update, context, get_text("duel.inspect.inaccessible"))
        return
    model = player_stats_read_model(chat_id, target["user_id"])
    if model is None:
        await send_and_schedule(update, context, get_text("duel.inspect.inaccessible"))
        return
    await send_and_schedule(update, context, format_player_stats_telegram(model, inspected=True))


async def _process_persistent_duel_fight(
    context, initiator_tg, target_username: str, chat_id: int,
    original_msg_id: int | None = None,
):
    """Translate Telegram's target selection into one authoritative start."""
    initiator = get_or_create_duel_user(initiator_tg, chat_id)
    opponent = get_duel_user_by_username(target_username, chat_id)
    opponent_id = opponent["user_id"] if opponent else -1
    try:
        started = start_persistent_duel(
            chat_id, initiator["user_id"], opponent_id,
            original_message_id=original_msg_id,
        )
    except Exception:
        logging.exception("Persistent duel start failed in chat %s", chat_id)
        return
    if started.success:
        try:
            await recover_persistent_duel_chat(chat_id, context.bot, job_queue=context.job_queue)
        except Exception:
            logging.exception("Persistent duel publication failed in chat %s", chat_id)
        return
    reason = started.reason
    if reason == "active_duel":
        message = get_text("duel.admission.active_duel")
    elif reason == "self_target":
        message = get_text("duel.admission.self_target")
    elif reason == "opponent_not_registered":
        message = get_text("duel.admission.opponent.not_found", username=target_username)
    elif reason == "initiator_no_points":
        message = get_text("duel.admission.initiator.no_points")
    elif reason == "opponent_no_points":
        message = get_text("duel.admission.opponent.no_points", title=format_user_title(opponent))
    elif reason in ("initiator_no_dick", "opponent_no_dick"):
        person = initiator if reason.startswith("initiator") else opponent
        message = get_text("duel.admission.participant.no_dick", title=format_user_title(person))
    else:
        logging.warning("Persistent duel admission rejected: %s in chat %s", reason, chat_id)
        return
    sent = await context.bot.send_message(chat_id, message, parse_mode="HTML")
    schedule_auto_delete(context, chat_id, [sent.message_id])


async def persistent_duel_action_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Reject legacy buttons and apply only chat/duel/turn scoped actions."""
    query = update.callback_query
    if query is None or not isinstance(query.data, str):
        return
    parts = query.data.split("_")
    if (len(parts) != 5 or parts[0] != "duel" or
            parts[1] not in ("strike", "block") or
            parts[2] not in TARGET_NAMES or
            not parts[3].isdecimal() or not parts[4].isdecimal()):
        await query.answer(get_text("duel.action_alert.stale_button"), show_alert=True)
        return
    action, zone, duel_id, turn_id = parts[1], parts[2], int(parts[3]), int(parts[4])
    if duel_id <= 0 or turn_id <= 0:
        await query.answer(get_text("duel.action_alert.stale_button"), show_alert=True)
        return
    chat_id = update.effective_chat.id
    try:
        operation = (submit_persistent_duel_attack if action == "strike"
                     else submit_persistent_duel_block)
        result = operation(chat_id, duel_id, query.from_user.id, turn_id, zone)
    except Exception:
        logging.exception("Persistent duel callback failed in chat %s", chat_id)
        await query.answer(get_text("duel.action_alert.stale_button"), show_alert=True)
        return
    if not result.accepted:
        if result.reason == "wrong_actor":
            key = f"duel.action_alert.{action}.wrong_actor"
        elif result.reason == "wrong_phase":
            key = f"duel.action_alert.{action}.wrong_phase"
        elif result.reason == "not_found":
            key = "duel.action_alert.no_active_duel"
        else:
            key = "duel.action_alert.turn_ended"
        await query.answer(get_text(key), show_alert=True)
        return
    await query.answer()
    try:
        await recover_persistent_duel_chat(chat_id, context.bot, job_queue=context.job_queue)
    except Exception:
        logging.exception("Persistent duel follow-up failed in chat %s", chat_id)

# ============================================================
# ТОП
# ============================================================

async def duel_top_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if (
        not update.message
        or not update.message.chat
    ):
        return

    chat_id = update.message.chat_id

    top = get_duel_top(
        chat_id=chat_id,
        limit=10,
        include_dwarf_name=True,
    )

    if not top:

        await send_and_schedule(
            update,
            context,
            get_text("duel.top.empty"),
        )

        return

    sort_label = (
        get_text("duel.top.sort_by_points")
        if TOP_SORT_BY == "points"
        else get_text("duel.top.sort_by_wins")
    )

    text = get_text(
        "duel.top.header",
        sort_label=sort_label,
    )

    for idx, row in enumerate(top, 1):

        username, display_name, wins, losses, points, *rest = row
        clean_name = format_user_title({
            "display_name": (display_name or username).lstrip("@") if (display_name or username) else None,
            "dwarf_name": rest[0] if rest else None,
        })

        text += get_text(
            "duel.top.row",
            index=idx,
            title=clean_name,
            points=points,
            wins=wins,
            losses=losses,
        )

    await send_and_schedule(
        update,
        context,
        text,
    )


# ============================================================
# УДАЛЕНИЕ ИГРОКА АДМИНОМ
# ============================================================

async def duel_delete_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if (
        not update.message
        or not update.message.from_user
        or not update.message.chat
    ):
        return

    user_id = update.message.from_user.id

    if user_id not in ADMIN_IDS:

        await send_and_schedule(
            update,
            context,
            get_text("duel.admin.delete.no_permission"),
        )

        return

    target_username = _extract_username(
        update,
        context,
    )

    if not target_username:

        await send_and_schedule(
            update,
            context,
            get_text("duel.admin.delete.usage"),
        )

        return

    chat_id = update.message.chat_id

    deleted = delete_duel_user_by_username(
        target_username,
        chat_id,
    )

    clean_target = target_username.lstrip("@")

    if deleted:

        await send_and_schedule(
            update,
            context,
            get_text("duel.admin.delete.success", username=clean_target),
        )

    else:

        await send_and_schedule(
            update,
            context,
            get_text("duel.admin.delete.not_found", username=clean_target),
        )


# ============================================================
# БОССЫ
# ============================================================

BOSS_PHASE_TIMEOUT = 10
BOSS_ROUND_PAUSE = 5

BOSS_JOIN_TIMEOUT = 30


BOSS_ZONES = ("head", "body", "dick")

ACTIVE_BOSS_BATTLES = {}

# ------------------------------------------------------------
# КЛАВИАТУРЫ
# ------------------------------------------------------------

def _boss_attack_keyboard(round_num: int):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                get_text("boss.keyboards.attack.head"),
                callback_data=f"boss_attack_head_{round_num}",
            ),
            InlineKeyboardButton(
                get_text("boss.keyboards.attack.body"),
                callback_data=f"boss_attack_body_{round_num}",
            ),
            InlineKeyboardButton(
                get_text("boss.keyboards.attack.dick"),
                callback_data=f"boss_attack_dick_{round_num}",
            ),
        ]
    ])


def _boss_block_keyboard(round_num: int):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                get_text("boss.keyboards.block.head"),
                callback_data=f"boss_block_head_{round_num}",
            ),
            InlineKeyboardButton(
                get_text("boss.keyboards.block.body"),
                callback_data=f"boss_block_body_{round_num}",
            ),
            InlineKeyboardButton(
                get_text("boss.keyboards.block.dick"),
                callback_data=f"boss_block_dick_{round_num}",
            ),
        ]
    ])


# ------------------------------------------------------------
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ------------------------------------------------------------

def _legacy_boss_phase_status(participant, phase):
    if not participant["alive"]:
        return "💀 погиб"

    if phase == "attack":
        return (
            "🟢 выбрал"
            if participant.get("attack") is not None
            else "🟡 выбирает"
        )

    if phase == "block":
        return (
            "🟢 выбрал"
            if participant.get("block") is not None
            else "🟡 выбирает"
        )

    return ""


async def _boss_render_phase(context, chat_id):
    battle = ACTIVE_BOSS_BATTLES.get(chat_id)

    if not battle:
        return

    text = _boss_phase_text(battle, BOSS_REQUIRED_HITS)

    if battle["phase"] == "attack":
        keyboard = _boss_attack_keyboard(
            battle["round"]
        )
    else:
        keyboard = _boss_block_keyboard(
            battle["round"]
        )

    try:
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=battle["message_id"],
            text=text,
            parse_mode="HTML",
            reply_markup=keyboard,
        )
    except Exception:
        logging.exception(
            "Не удалось обновить фазу битвы с боссом "
            "в чате %s",
            chat_id,
        )


def _boss_cancel_timer(battle):
    task = battle.get("phase_task")
    battle["phase_task"] = None

    if not task or task.done():
        return

    if task is asyncio.current_task():
        return

    task.cancel()


def _boss_auto_zone():
    return random.choice(BOSS_ZONES)


async def _boss_auto_choose_for_zazevasha(
    context,
    chat_id,
    battle,
    participant,
    phase,
):
    if not participant["alive"]:
        return

    title = _boss_player_title(participant)
    zone = _boss_auto_zone()

    if phase == "attack":
        if participant.get("attack") is not None:
            return

        participant["attack"] = zone
        action_text = get_text(
            "boss.timeout.auto_attack",
            title=title,
            zone=BOSS_ZONE_NAMES[zone],
        )
    else:
        if participant.get("block") is not None:
            return

        participant["block"] = zone
        action_text = get_text(
            "boss.timeout.auto_block",
            title=title,
            zone=BOSS_ZONE_NAMES[zone],
        )

    try:
        timeout_msg = await context.bot.send_message(
            chat_id=chat_id,
            text=action_text,
            parse_mode="HTML",
        )
        schedule_auto_delete(
            context,
            chat_id,
            [timeout_msg.message_id],
        )
    except Exception:
        logging.exception(
            "Не удалось отправить сообщение автодействия босса "
            "в чат %s",
            chat_id,
        )


# ------------------------------------------------------------
# НАЧАЛО НОВОГО РАУНДА
# ------------------------------------------------------------

async def _boss_start_round(
    context,
    chat_id,
):
    battle = ACTIVE_BOSS_BATTLES.get(chat_id)

    if not battle:
        return

    alive = _boss_alive_players(battle)

    if not alive:
        await _boss_finish_defeat(
            context,
            chat_id,
        )
        return

    boss_attack = random.choice(
        BOSS_ZONES
    )

    boss_block = random.choice(
        BOSS_ZONES
    )

    _begin_boss_round(
        battle,
        boss_attack,
        boss_block,
    )

    await _boss_render_phase(
        context,
        chat_id,
    )

    _boss_cancel_timer(battle)

    battle["phase_task"] = asyncio.create_task(
        _boss_phase_timer(
            context,
            chat_id,
            battle["round"],
            "attack",
        )
    )


# ------------------------------------------------------------
# ТАЙМЕР ФАЗЫ
# ------------------------------------------------------------

async def _boss_phase_timer(
    context,
    chat_id,
    round_num,
    phase,
):
    current_task = asyncio.current_task()

    try:
        await asyncio.sleep(BOSS_PHASE_TIMEOUT)
    except asyncio.CancelledError:
        return

    battle = ACTIVE_BOSS_BATTLES.get(chat_id)

    if not battle:
        return

    finish_defeat = False
    switch_to_block = False
    resolve_round = False

    async with battle["lock"]:
        # Защита от старых/дублирующихся таймеров.
        # Если callback уже заменил phase_task, этот таймер больше
        # не имеет права двигать бой дальше.
        if battle.get("phase_task") is not current_task:
            return

        if (
            battle["round"] != round_num
            or battle["phase"] != phase
        ):
            return

        alive = _boss_alive_players(battle)

        if not alive:
            finish_defeat = True
            battle["phase_task"] = None
        else:
            # Если кто-то зазевался, делаем ему случайный ход.
            for participant in alive:
                field = "attack" if phase == "attack" else "block"

                if participant.get(field) is None:
                    await _boss_auto_choose_for_zazevasha(
                        context,
                        chat_id,
                        battle,
                        participant,
                        phase,
                    )

            if phase == "attack":
                battle["phase"] = "block"
                switch_to_block = True
                battle["phase_task"] = None

            elif phase == "block":
                resolve_round = True
                battle["phase_task"] = None

    if finish_defeat:
        await _boss_finish_defeat(
            context,
            chat_id,
        )
        return

    if switch_to_block:
        current_battle = ACTIVE_BOSS_BATTLES.get(chat_id)

        if not current_battle:
            return

        await _boss_render_phase(
            context,
            chat_id,
        )

        current_battle = ACTIVE_BOSS_BATTLES.get(chat_id)

        if not current_battle:
            return

        async with current_battle["lock"]:
            if (
                current_battle["round"] != round_num
                or current_battle["phase"] != "block"
            ):
                return

            current_battle["phase_task"] = asyncio.create_task(
                _boss_phase_timer(
                    context,
                    chat_id,
                    round_num,
                    "block",
                )
            )

        return

    if resolve_round:
        await _boss_resolve_round(
            context,
            chat_id,
        )


# ------------------------------------------------------------
# CALLBACK БОССА
# ------------------------------------------------------------

async def boss_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.callback_query

    if not query or not query.data:
        return

    chat_id = update.effective_chat.id

    battle = ACTIVE_BOSS_BATTLES.get(chat_id)

    if not battle:
        await query.answer(
            get_text("boss.callback.battle_finished"),
            show_alert=True,
        )
        return

    async with battle["lock"]:
        callback_data = query.data
        user_id = query.from_user.id

        if callback_data == "boss_join":
            if battle["phase"] != "join":
                await query.answer(
                    get_text("boss.callback.join.already_started"),
                    show_alert=True,
                )
                return

            if user_id in battle["participants"]:
                await query.answer(
                    get_text("boss.callback.join.already_participating"),
                    show_alert=True,
                )
                return

            battle["participants"][user_id] = _boss_make_participant(
                query.from_user,
                chat_id,
            )

            await query.answer(
                get_text("boss.callback.join.joined")
            )

            try:
                await context.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=battle["message_id"],
                    text=get_text(
                        "boss.callback.join.progress",
                        boss_name=battle["boss"]["name"],
                        participants=len(battle["participants"]),
                    ),
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup([
                        [
                            InlineKeyboardButton(
                                get_text("boss.join.button"),
                                callback_data="boss_join",
                            )
                        ]
                    ]),
                )
            except Exception:
                pass

            return

        if callback_data.startswith("boss_attack_"):
            if battle["phase"] != "attack":
                await query.answer(
                    get_text("boss.callback.attack_phase_closed"),
                    show_alert=True,
                )
                return

            participant = battle["participants"].get(
                user_id
            )

            if not participant:
                await query.answer(
                    get_text("boss.callback.not_participant"),
                    show_alert=True,
                )
                return

            if not participant["alive"]:
                await query.answer(
                    get_text("boss.callback.dead"),
                    show_alert=True,
                )
                return

            parts = callback_data.split("_")

            if len(parts) != 4:
                await query.answer(
                    get_text("boss.callback.stale_button"),
                    show_alert=True,
                )
                return

            zone = parts[2]

            try:
                button_round = int(parts[3])
            except ValueError:
                await query.answer(
                    get_text("boss.callback.stale_button"),
                    show_alert=True,
                )
                return

            if button_round != battle["round"]:
                await query.answer(
                    get_text("boss.callback.round_finished"),
                    show_alert=True,
                )
                return

            if zone not in BOSS_ZONES:
                await query.answer(
                    get_text("boss.callback.unknown_zone"),
                    show_alert=True,
                )
                return

            should_switch_to_block = _record_boss_attack_choice(
                battle,
                participant,
                zone,
            )

            await query.answer(
                get_text("boss.callback.attack_ack", zone=BOSS_ZONE_NAMES[zone])
            )

            await _boss_render_phase(
                context,
                chat_id,
            )

            if should_switch_to_block:
                _boss_cancel_timer(battle)

                _enter_boss_block_phase(battle)

                await _boss_render_phase(
                    context,
                    chat_id,
                )

                battle["phase_task"] = asyncio.create_task(
                    _boss_phase_timer(
                        context,
                        chat_id,
                        battle["round"],
                        "block",
                    )
                )

            return

        if callback_data.startswith("boss_block_"):
            if battle["phase"] != "block":
                await query.answer(
                    get_text("boss.callback.block_phase_closed"),
                    show_alert=True,
                )
                return

            participant = battle["participants"].get(
                user_id
            )

            if not participant:
                await query.answer(
                    get_text("boss.callback.not_participant"),
                    show_alert=True,
                )
                return

            if not participant["alive"]:
                await query.answer(
                    get_text("boss.callback.dead"),
                    show_alert=True,
                )
                return

            parts = callback_data.split("_")

            if len(parts) != 4:
                await query.answer(
                    get_text("boss.callback.stale_button"),
                    show_alert=True,
                )
                return

            zone = parts[2]

            try:
                button_round = int(parts[3])
            except ValueError:
                await query.answer(
                    get_text("boss.callback.stale_button"),
                    show_alert=True,
                )
                return

            if button_round != battle["round"]:
                await query.answer(
                    get_text("boss.callback.round_finished"),
                    show_alert=True,
                )
                return

            if zone not in BOSS_ZONES:
                await query.answer(
                    get_text("boss.callback.unknown_zone"),
                    show_alert=True,
                )
                return

            should_resolve = _record_boss_block_choice(
                battle,
                participant,
                zone,
            )

            await query.answer(
                get_text("boss.callback.block_ack", zone=BOSS_ZONE_NAMES[zone])
            )

            await _boss_render_phase(
                context,
                chat_id,
            )

            if should_resolve:
                _boss_cancel_timer(battle)

                # Нельзя await-ить _boss_resolve_round здесь:
                # callback сам ещё держит battle["lock"], а
                # _boss_resolve_round пытается взять тот же lock.
                #
                # Создаём задачу сейчас — она начнёт выполняться
                # после выхода callback из async with.
                asyncio.create_task(
                    _boss_resolve_round(
                        context,
                        chat_id,
                    )
                )

            return


# ------------------------------------------------------------
# РЕЗУЛЬТАТ РАУНДА
# ------------------------------------------------------------

async def _boss_resolve_round(
    context,
    chat_id,
):
    battle = ACTIVE_BOSS_BATTLES.get(chat_id)

    if not battle:
        return

    async with battle["lock"]:
        # Пока держим lock, только рассчитываем и сохраняем состояние.
        # Telegram API, sleep и финализацию выполняем после выхода из lock.
        if battle["phase"] != "block":
            return

        # Сразу переводим раунд в resolving после расчёта ниже.
        # Это не даёт второму callback/таймеру разрешить тот же раунд.
        boss_attack = battle["boss_attack"]
        boss_block = battle["boss_block"]
        projected_hits = battle["hits"]

        for participant in battle["participants"].values():
            if not participant["alive"]:
                continue

            attack = participant.get("attack")

            # Защита от старого/битого таймера: зазевавшемуся
            # участнику всё равно назначаем ход.
            if attack not in BOSS_ZONES:
                attack = _boss_auto_zone()
                participant["attack"] = attack

            if attack != boss_block:
                projected_hits += 1

                if projected_hits >= BOSS_REQUIRED_HITS:
                    break

            block = participant.get("block")

            if block not in BOSS_ZONES:
                block = _boss_auto_zone()
                participant["block"] = block

        round_result = _apply_boss_round_result(
            battle,
            BOSS_REQUIRED_HITS,
        )
        results = []

        for participant_result in round_result["round_results"]:
            participant = participant_result["participant"]
            attack = participant_result["attack"]
            block = participant_result["block"]
            attack_result = get_text(
                "boss.round.attack_result.hit"
                if participant_result["hit"]
                else "boss.round.attack_result.miss"
            )

            title = _boss_player_title(participant)

            if participant_result["boss_responded"]:
                block_result = get_text(
                    "boss.round.block_result.survived"
                    if participant_result["survived"]
                    else "boss.round.block_result.dead"
                )
                results.append(
                    get_text(
                        "boss.round.participant_result",
                        title=title,
                        attack_zone=BOSS_ZONE_NAMES[attack],
                        attack_result=attack_result,
                        block_zone=BOSS_ZONE_NAMES[block],
                        block_result=block_result,
                    )
                )
            else:
                results.append(
                    get_text(
                        "boss.round.finishing_participant_result",
                        title=title,
                        attack_zone=BOSS_ZONE_NAMES[attack],
                        attack_result=attack_result,
                    )
                )

        alive_after = round_result["alive_after"]

        boss_responded = any(
            participant_result["boss_responded"]
            for participant_result in round_result["round_results"]
        )
        summary_key = (
            "boss.round.finishing_summary"
            if round_result["victory"] and not boss_responded
            else "boss.round.summary"
        )
        text = get_text(
            summary_key,
            round=battle["round"],
            boss_attack=BOSS_ZONE_NAMES[boss_attack],
            boss_block=BOSS_ZONE_NAMES[boss_block],
            results="\n\n".join(results),
            hits=battle["hits"],
            required_hits=BOSS_REQUIRED_HITS,
            alive_after=alive_after,
            total=len(battle["participants"]),
        )

        outcome = round_result["outcome"]

    try:
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=battle["message_id"],
            text=text,
            parse_mode="HTML",
        )
    except Exception:
        logging.exception(
            "Не удалось показать результат раунда босса "
            "в чате %s",
            chat_id,
        )

    if outcome == "victory":
        await asyncio.sleep(2)
        await _boss_finish_victory(
            context,
            chat_id,
        )
        return

    if outcome == "defeat":
        await asyncio.sleep(2)
        await _boss_finish_defeat(
            context,
            chat_id,
        )
        return

    await asyncio.sleep(BOSS_ROUND_PAUSE)

    current_battle = ACTIVE_BOSS_BATTLES.get(chat_id)

    if not current_battle:
        return

    await _boss_start_round(
        context,
        chat_id,
    )


# ------------------------------------------------------------
# ФИНАЛЬНАЯ СТАТИСТИКА БИТВЫ
# ------------------------------------------------------------



def _legacy_boss_battle_hero(participants):
    alive = [p for p in participants if p["alive"]]

    if not participants:
        return None

    return max(
        participants,
        key=lambda p: (
            p.get("hits", 0),
            p.get("blocks", 0),
            p.get("rounds_survived", 0),
            1 if p in alive else 0,
        ),
    )




async def _boss_send_final_report(
    context,
    chat_id,
    battle,
    victory,
):
    try:
        text = _boss_final_report(
            battle,
            victory,
        )
    except Exception:
        logging.exception(
            "Ошибка формирования финального отчёта босса "
            "в чате %s",
            chat_id,
        )

        # Финальный отчёт не должен исчезать из-за одной ошибки
        # в красивой статистике.
        text = get_text("boss.report.fallback")

    try:
        item_loot_text = _maybe_award_boss_item(chat_id, battle)
    except Exception:
        logging.exception("Ошибка item loot после боя с боссом в чате %s", chat_id)
    else:
        if item_loot_text is not None:
            text += f"\n\n{item_loot_text}"

    chunks = []
    current = ""

    for paragraph in text.split("\n\n"):
        candidate = (
            paragraph
            if not current
            else current + "\n\n" + paragraph
        )

        if len(candidate) <= 3900:
            current = candidate
            continue

        if current:
            chunks.append(current)
            current = ""

        while len(paragraph) > 3900:
            chunks.append(paragraph[:3900])
            paragraph = paragraph[3900:]

        current = paragraph

    if current:
        chunks.append(current)

    if not chunks:
        chunks = [text]

    first_message_id = battle.get("message_id")

    try:
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=first_message_id,
            text=chunks[0],
            parse_mode="HTML",
        )
    except Exception:
        logging.exception(
            "Не удалось отредактировать финальное сообщение "
            "босса в чате %s",
            chat_id,
        )

        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text=chunks[0],
                parse_mode="HTML",
            )
        except Exception:
            logging.exception(
                "Не удалось отправить финальный отчёт босса "
                "в чат %s",
                chat_id,
            )

    for chunk in chunks[1:]:
        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text=chunk,
                parse_mode="HTML",
            )
        except Exception:
            logging.exception(
                "Не удалось отправить часть финального отчёта "
                "босса в чат %s",
                chat_id,
            )
            break


# ------------------------------------------------------------
# ПОБЕДА
# ------------------------------------------------------------

async def _boss_finish_victory(
    context,
    chat_id,
):
    battle = ACTIVE_BOSS_BATTLES.pop(
        chat_id,
        None,
    )

    if not battle:
        return

    _boss_cancel_timer(battle)

    try:
        increment_monthly_bosses_killed(chat_id)
    except Exception:
        logging.exception("Не удалось записать месячную победу над боссом в чате %s", chat_id)

    for participant in battle["participants"].values():
        if not participant["alive"]:
            continue

        user_id = participant["tg_user"].id

        try:
            reward_boss_victory(
                user_id=user_id,
                chat_id=chat_id,
            )
        except Exception:
            logging.exception(
                "Ошибка награды за победу над боссом"
            )

    try:
        await _boss_send_final_report(
            context,
            chat_id,
            battle,
            victory=True,
        )
    except Exception:
        logging.exception(
            "Критическая ошибка финализации победы босса "
            "в чате %s",
            chat_id,
        )


# ------------------------------------------------------------
# ПОРАЖЕНИЕ
# ------------------------------------------------------------

async def _boss_finish_defeat(
    context,
    chat_id,
):
    battle = ACTIVE_BOSS_BATTLES.pop(
        chat_id,
        None,
    )

    if not battle:
        return

    _boss_cancel_timer(battle)

    try:
        await _boss_send_final_report(
            context,
            chat_id,
            battle,
            victory=False,
        )
    except Exception:
        logging.exception(
            "Критическая ошибка финализации поражения босса "
            "в чате %s",
            chat_id,
        )


# ------------------------------------------------------------
# СОЗДАНИЕ БОЕВОГО УЧАСТНИКА
# ------------------------------------------------------------

def _boss_make_participant(
    tg_user,
    chat_id,
):
    user_data = get_or_create_duel_user(
        tg_user,
        chat_id,
    )

    return {
        "tg_user": tg_user,
        "data": user_data,
        "attack": None,
        "block": None,
        "alive": True,
        "hits": 0,
        "misses": 0,
        "blocks": 0,
        "rounds_survived": 0,
        "death_round": None,
        "death_by_zone": None,
        "death_defended_zone": None,
        "death_attack_zone": None,
    }


def _boss_tg_user_from_registration(row):
    user_id, username, first_name, last_name = row

    # telegram.User достаточно для существующей функции
    # get_or_create_duel_user.
    from telegram import User

    return User(
        id=int(user_id),
        first_name=first_name or "",
        is_bot=False,
        last_name=last_name,
        username=username,
    )


# ------------------------------------------------------------
# ЗАПУСК БИТВЫ С БОССОМ
# ------------------------------------------------------------

async def _start_boss_battle(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    include_registrations=False,
):
    """
    Запускает набор участников на битву с боссом.

    При ежедневном запуске в 13:37 include_registrations=True:
    все записавшиеся через /boss_reg автоматически становятся
    участниками, как если бы они сами вступили в набор.
    """
    if chat_id in ACTIVE_BOSS_BATTLES:
        return False

    registered_rows = (
        _boss_get_registered_users(chat_id)
        if include_registrations
        else []
    )

    boss = random.choice(BOSSES)

    bot_msg = await context.bot.send_message(
        chat_id=chat_id,
        text=get_text(
            "boss.battle.spawn",
            boss_name=boss["name"],
            required_hits=BOSS_REQUIRED_HITS,
        ),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    get_text("boss.join.button"),
                    callback_data="boss_join",
                )
            ]
        ]),
    )

    battle = {
        "boss": boss,
        "participants": {},
        "hits": 0,
        "round": 0,
        "phase": "join",
        "message_id": bot_msg.message_id,
        "phase_task": None,
        "lock": asyncio.Lock(),
        "boss_attack": None,
        "boss_block": None,
    }

    for row in registered_rows:
        tg_user = _boss_tg_user_from_registration(row)

        if tg_user.id in battle["participants"]:
            continue

        try:
            battle["participants"][tg_user.id] = _boss_make_participant(
                tg_user,
                chat_id,
            )
        except Exception:
            logging.exception(
                "Не удалось добавить предварительно "
                "зарегистрированного участника %s в чате %s",
                tg_user.id,
                chat_id,
            )

    ACTIVE_BOSS_BATTLES[chat_id] = battle

    if battle["participants"]:
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=battle["message_id"],
                text=get_text(
                    "boss.battle.pre_registered",
                    boss_name=boss["name"],
                    participants=len(battle["participants"]),
                ),
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton(
                            get_text("boss.join.button"),
                            callback_data="boss_join",
                        )
                    ]
                ]),
            )
        except Exception:
            pass

    battle["phase_task"] = asyncio.create_task(
        _boss_join_timer(
            context,
            chat_id,
        )
    )

    return True


# ------------------------------------------------------------
# РЕГИСТРАЦИЯ /boss_reg
# ------------------------------------------------------------

async def boss_reg_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if (
        not update.message
        or not update.message.from_user
        or not update.message.chat
    ):
        return

    chat = update.message.chat

    if chat.type not in {"group", "supergroup"}:
        await send_and_schedule(
            update,
            context,
            get_text("boss.registration.group_only"),
        )
        return

    chat_id = chat.id

    if not _boss_registration_is_open():
        await send_and_schedule(
            update,
            context,
            get_text("boss.registration.closed"),
        )
        return

    if chat_id in ACTIVE_BOSS_BATTLES:
        await send_and_schedule(
            update,
            context,
            get_text("boss.registration.active"),
        )
        return

    # Команда участника также означает, что бот должен считать
    # этот чат активным для ежедневного босса.
    set_boss_enabled(chat_id, True)

    added = _boss_register_user(
        chat_id,
        update.message.from_user,
    )
    participant_count = len(_boss_get_registered_users(chat_id))
    participant_count_text = get_text(
        "boss.registration.participant_count",
        count=participant_count,
    )

    if added:
        await send_and_schedule(
            update,
            context,
            f'{get_text("boss.registration.registered")}\n\n{participant_count_text}',
            parse_mode="HTML",
        )
    else:
        await send_and_schedule(
            update,
            context,
            f'{get_text("boss.registration.already_registered")}\n\n{participant_count_text}',
            parse_mode="HTML",
        )


# ------------------------------------------------------------
# РУЧНОЙ ЗАПУСК /boss
# ------------------------------------------------------------

async def boss_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if (
        not update.message
        or not update.message.from_user
        or not update.message.chat
    ):
        return

    if update.message.from_user.id not in ADMIN_IDS:
        await send_and_schedule(
            update,
            context,
            get_text("boss.command.forbidden"),
        )
        return

    chat_id = update.message.chat_id

    if chat_id > 0:
        await send_and_schedule(
            update,
            context,
            get_text("boss.command.group_only"),
        )
        return

    if chat_id in ACTIVE_BOSS_BATTLES:
        await send_and_schedule(
            update,
            context,
            get_text("boss.command.active"),
        )
        return

    set_boss_enabled(chat_id, True)

    try:
        started = await _start_boss_battle(
            context,
            chat_id,
            include_registrations=False,
        )
    except (BadRequest, Forbidden) as exc:
        set_boss_enabled(chat_id, False)
        logging.warning(
            "Не удалось вручную запустить босса в чате %s: %s",
            chat_id,
            exc,
        )
        return

    if started:
        return


# ------------------------------------------------------------
# ЕЖЕДНЕВНЫЙ БОСС
# ------------------------------------------------------------

async def boss_daily_job(context: ContextTypes.DEFAULT_TYPE):
    """
    Ежедневно запускает битву в 13:37.

    Все пользователи, записавшиеся через /boss_reg до 13:37,
    автоматически становятся участниками. Остальные могут
    присоединиться через кнопку в течение окна набора.
    """
    chats = set(get_all_chats())
    chats.update(_boss_get_registered_chat_ids())

    started = 0
    skipped = 0
    disabled = 0

    for chat_id in chats:
        if chat_id in ACTIVE_BOSS_BATTLES:
            skipped += 1
            continue

        if not is_boss_enabled(chat_id):
            skipped += 1
            continue

        try:
            if await _start_boss_battle(
                context,
                chat_id,
                include_registrations=True,
            ):
                _boss_clear_registrations(chat_id)
                started += 1
            else:
                skipped += 1

        except (Forbidden, BadRequest) as exc:
            set_boss_enabled(chat_id, False)
            disabled += 1
            logging.warning(
                "Отключаем ежедневного босса для чата %s: %s",
                chat_id,
                exc,
            )

        except Exception:
            logging.exception(
                "Ошибка запуска ежедневного босса в чате %s",
                chat_id,
            )

    logging.info(
        "Ежедневный босс: запущено=%s, пропущено=%s, "
        "отключено=%s, всего=%s",
        started,
        skipped,
        disabled,
        len(chats),
    )


async def _boss_join_timer(
    context,
    chat_id,
):
    try:
        await asyncio.sleep(BOSS_JOIN_TIMEOUT)
    except asyncio.CancelledError:
        return

    battle = ACTIVE_BOSS_BATTLES.get(chat_id)

    if not battle:
        return

    async with battle["lock"]:
        if battle["phase"] != "join":
            return

        if not battle["participants"]:
            ACTIVE_BOSS_BATTLES.pop(
                chat_id,
                None,
            )

            try:
                await context.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=battle["message_id"],
                    text=get_text(
                        "boss.battle.empty_join_timeout",
                        boss_name=battle["boss"]["name"],
                    ),
                    parse_mode="HTML",
                )
            except Exception:
                pass

            return

        battle["phase"] = "attack"

        await _boss_start_round(
            context,
            chat_id,
        )
