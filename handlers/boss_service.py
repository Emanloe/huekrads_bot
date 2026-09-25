"""Shared in-memory boss actions for Telegram callbacks and Mini App."""

import asyncio
from dataclasses import dataclass

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from text_resources import get_text


@dataclass(frozen=True)
class BossActionResult:
    accepted: bool
    code: str


async def apply_boss_action(context, chat_id: int, user_id: int, intent: str,
                            *, zone: str | None = None, tg_user=None,
                            expected_battle_id: str | None = None,
                            expected_round: int | None = None,
                            expected_phase: str | None = None,
                            on_accepted=None,
                            dedupe_same_choice: bool = False) -> BossActionResult:
    """Validate and apply one action under the canonical battle lock.

    Telegram owns the timers and publication. Both frontends enter here.
    """
    from handlers import duel

    battle = duel.ACTIVE_BOSS_BATTLES.get(chat_id)
    if battle is None:
        return BossActionResult(False, "no_active_battle")
    async with battle["lock"]:
        if duel.ACTIVE_BOSS_BATTLES.get(chat_id) is not battle:
            return BossActionResult(False, "stale_battle")
        if expected_battle_id is not None and battle.get("battle_id") != expected_battle_id:
            return BossActionResult(False, "stale_battle")
        if expected_phase is not None and battle["phase"] != expected_phase:
            return BossActionResult(False, "stale_phase")

        if intent == "join":
            if battle["phase"] != "join":
                return BossActionResult(False, "recruitment_closed")
            if expected_round is not None and battle["round"] != expected_round:
                return BossActionResult(False, "stale_round")
            if user_id in battle["participants"]:
                return BossActionResult(False, "already_joined")
            if tg_user is None or tg_user.id != user_id:
                return BossActionResult(False, "not_registered")
            battle["participants"][user_id] = duel._boss_make_participant(tg_user, chat_id)
            if on_accepted is not None:
                await on_accepted()
            try:
                await context.bot.edit_message_text(
                    chat_id=chat_id, message_id=battle["message_id"],
                    text=get_text("boss.callback.join.progress",
                                  boss_name=battle["boss"]["name"],
                                  participants=len(battle["participants"])),
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton(get_text("boss.join.button"),
                                             callback_data="boss_join")
                    ]]),
                )
            except Exception:
                pass
            return BossActionResult(True, "joined")

        if intent not in ("attack", "block"):
            return BossActionResult(False, "invalid_action")
        if battle["phase"] != intent:
            return BossActionResult(False, "stale_phase")
        participant = battle["participants"].get(user_id)
        if participant is None:
            return BossActionResult(False, "not_participant")
        if not participant["alive"]:
            return BossActionResult(False, "eliminated")
        if expected_round is not None and battle["round"] != expected_round:
            return BossActionResult(False, "stale_round")
        if zone not in duel.BOSS_ZONES:
            return BossActionResult(False, "invalid_action")
        if dedupe_same_choice and participant.get(intent) == zone:
            return BossActionResult(False, "already_acted")
        if intent == "attack":
            advance = duel._record_boss_attack_choice(battle, participant, zone)
        else:
            advance = duel._record_boss_block_choice(battle, participant, zone)
        if on_accepted is not None:
            await on_accepted()
        await duel._boss_render_phase(context, chat_id)
        if advance:
            duel._boss_cancel_timer(battle)
            if intent == "attack":
                duel._enter_boss_block_phase(battle)
                await duel._boss_render_phase(context, chat_id)
                duel._boss_schedule_phase_timer(context, chat_id, battle, "block")
            else:
                # Resolution takes the same lock after this operation releases it.
                asyncio.create_task(duel._boss_resolve_round(context, chat_id))
        return BossActionResult(True, "accepted")
