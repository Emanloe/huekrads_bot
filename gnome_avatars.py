"""Persistent chat-scoped cosmetic gnome assignment and fixed media catalog."""

import hashlib
import secrets

from database import get_db


GNOME_FILE_IDS = {
    "gnome_00": "AgACAgIAAxkBAAPaarZGZ0LcyUlK8_7as-niVWw-EbIAApYbaxt2IbBJ2k2XK8ElkUMBAAMCAAN5AAM9BA",
    "gnome_01": "AgACAgIAAxkBAAPcarZULnrKuWMKOtosgqroZQKMDqMAAukbaxt2IbBJ6ovXlnzZiDIBAAMCAAN4AAM9BA",
    "gnome_02": "AgACAgIAAxkBAAPdarZULpkcnzswX5qchzGAoa2_XO4AAuobaxt2IbBJjO9Oc7zXz-ABAAMCAAN4AAM9BA",
    "gnome_03": "AgACAgIAAxkBAAPearZULu1WMsikSaXmq-xIc8zewVAAAu0baxt2IbBJK5Mk0XhO92UBAAMCAAN4AAM9BA",
    "gnome_04": "AgACAgIAAxkBAAPfarZULqToimhBC4PhKTdc39VH3gYAAuwbaxt2IbBJuKbfOY8oPbcBAAMCAAN4AAM9BA",
    "gnome_05": "AgACAgIAAxkBAAPgarZULqu05wb-iog5HsejGfX62BoAAusbaxt2IbBJOt65YrnLJcYBAAMCAAN4AAM9BA",
    "gnome_06": "AgACAgIAAxkBAAPharZULnkiYNqZI0E1_TM6usZHlLEAAu4baxt2IbBJ7R13AgkF1HkBAAMCAAN4AAM9BA",
    "gnome_07": "AgACAgIAAxkBAAPiarZULnig_lN0DAm2CpQ4QCxPyYMAAvAbaxt2IbBJx2Zdb-5GOMwBAAMCAAN4AAM9BA",
    "gnome_08": "AgACAgIAAxkBAAPjarZULhlRW246UV5qzNXB2X4bic4AAu8baxt2IbBJA8fYz-ScoBgBAAMCAAN4AAM9BA",
    "gnome_09": "AgACAgIAAxkBAAPkarZULukMv-4XGppZT_DeG6u9DKAAAvEbaxt2IbBJUx30mDJiuY8BAAMCAAN4AAM9BA",
}
GNOME_VARIANTS = tuple(GNOME_FILE_IDS)
DEFAULT_GNOME_VARIANT = "gnome_00"


def gnome_image_version(variant: str) -> str:
    return hashlib.sha256(GNOME_FILE_IDS[variant].encode()).hexdigest()


def gnome_image_url(variant: str) -> str:
    return f"/media/gnome/{variant}?v={gnome_image_version(variant)}"


def get_or_assign_gnome_variant(chat_id: int, user_id: int) -> str | None:
    """Choose once for an existing player; serialize first access across processes."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("BEGIN IMMEDIATE")
        row = cursor.execute(
            "SELECT gnome_variant FROM duel_users WHERE chat_id = ? AND user_id = ?",
            (chat_id, user_id),
        ).fetchone()
        if row is None:
            return None
        if row[0] is not None:
            return row[0]
        variant = secrets.choice(GNOME_VARIANTS)
        cursor.execute(
            "UPDATE duel_users SET gnome_variant = ? WHERE chat_id = ? AND user_id = ?",
            (variant, chat_id, user_id),
        )
        return variant
