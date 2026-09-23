import json
import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")

# Список ID администраторов (преобразован в set для O(1) поиска)
raw_admin_ids = os.getenv("ADMIN_IDS", os.getenv("ADMIN_ID", ""))
ADMIN_IDS = {int(x.strip()) for x in raw_admin_ids.split(",") if x.strip().isdigit()}

# --- GIF и Медиа файлы ---
_WEATHER_CONTENT_PATH = Path(__file__).resolve().parent / "data" / "weather_content.json"
with open(_WEATHER_CONTENT_PATH, encoding="utf-8") as _content_file:
    _weather_content = json.load(_content_file)

OREL_GIF_IDS = _weather_content["orel_gif_ids"]
RUSSIA_GIF_FILE_ID = _weather_content["russia_gif_file_id"]
MOSCOW_PHOTO_IDS = _weather_content["moscow_photo_ids"]
SPB_PHOTO_IDS = _weather_content["spb_photo_ids"]
NSK_PHOTO_IDS = _weather_content["nsk_photo_ids"]
PERM_PHOTO_IDS = _weather_content["perm_photo_ids"]
WEATHER_STUB_PHOTO_ID = _weather_content.get("stub_photo_file_id") or ""

_LET_DO_CONTENT_PATH = Path(__file__).resolve().parent / "data" / "let_do_content.json"
with open(_LET_DO_CONTENT_PATH, encoding="utf-8") as _let_do_file:
    _let_do_content = json.load(_let_do_file)

LET_DO_STICKER_IDS = _let_do_content["sticker_ids"]

_DUEL_CONTENT_PATH = Path(__file__).resolve().parent / "data" / "duel_content.json"
with open(_DUEL_CONTENT_PATH, encoding="utf-8") as _duel_file:
    _duel_content = json.load(_duel_file)

WINNER_100_PTS_GIF = _duel_content["winner_100_pts_gif"]

_BIRTHDAY_CONTENT_PATH = Path(__file__).resolve().parent / "data" / "birthday_content.json"
with open(_BIRTHDAY_CONTENT_PATH, encoding="utf-8") as _birthday_file:
    _birthday_content = json.load(_birthday_file)

BIRTHDAY_GIF_ID = _birthday_content["birthday_gif_id"]

# --- Настройки планировщика ---
GAME_HOUR = 10
GAME_MINUTE = 0
DUEL_TIMEZONE = "Europe/Moscow"

# --- Настройки Гномьей дуэли ---
DAILY_START_POINTS = 20
MAX_DAILY_POINTS = 100
WIN_POINTS = 10
LOSS_POINTS = 5
DICK_STEAL_CHANCE = 0.20  # 20%
DICK_STEAL_CHANCE_PER_WIN = 0.01  # +1% за победу победителя в течение дня
BERSERK_CHANCE = 0.001  # 0.1% на завершённую дуэль
DUEL_POST_MESSAGE_CHANCE = 0.10  # 10% на пост-дуэльный комментарий
DUEL_ITEM_STEAL_CHANCE = 0.05  # 5% при сработавшей краже хуя
DUEL_ITEM_DROP_CHANCE = 0.50  # 50% потерять предмет из непустого инвентаря
BOSS_ITEM_DROP_CHANCE = 0.50  # 50% на один предмет одному выжившему
DUEL_ITEM_EVENT_CHANCE = 0.10  # 10% на каждую почасовую проверку чата
DIG_FIND_CHANCE = 0.20
DUEL_ITEM_EVENT_CHECK_MINUTES = 60
DUEL_WIN_CHANCE = 0.50    # 50%
TOP_SORT_BY = "wins"      # "wins" | "net_wins" | "points"
