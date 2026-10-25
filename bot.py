"""
Telegram-бот для анонсів боїв з автоматичним таймером зворотного відліку.

Як це працює:
1. Ви (адмін) надсилаєте боту в особисті повідомлення ФОТО афіші з підписом
   у форматі (кожен рядок — своя стрічка):

       Михайло Діденко vs Данило Коваленко
       21.11.2026 20:00
       Львів, СК «Грація»

   Рядок 1 — назва бою (будь-який текст)
   Рядок 2 — дата й час у форматі ДД.ММ.РРРР ГГ:ХХ
   Рядок 3 — місце проведення (необов'язково)

2. Бот одразу публікує це фото у вашому каналі з підписом і таймером:
       "До бою залишилось: 12 дн. 05 год. 30 хв."

3. Раз на деякий час (інтервал налаштовується, за замовчуванням — 30 хв,
   а в останню добу перед боєм — кожні 5 хв) бот сам редагує підпис цього
   поста, оновлюючи таймер. Коли час бою настав — підпис змінюється на
   "🔴 Бій почався!" і оновлення зупиняються.

Дані про активні анонси зберігаються у файлі fights.json поруч зі скриптом,
тож бот переживає перезапуск.
"""

import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message
from aiogram.exceptions import TelegramBadRequest
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.environ["BOT_TOKEN"]            # токен від @BotFather
CHANNEL_ID = os.environ["CHANNEL_ID"]          # напр. "@my_channel" або "-1001234567890"
ADMIN_IDS = {
    int(x) for x in os.environ.get("ADMIN_IDS", "").split(",") if x.strip()
}  # ваш Telegram user id (і, за бажання, інших людей, яким можна постити)

DATA_FILE = Path(__file__).parent / "fights.json"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("fightbot")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()


@dataclass
class Fight:
    chat_id: str
    message_id: int
    title: str
    location: str
    event_ts: float          # unix timestamp дати бою
    finished: bool = False

    def to_dict(self):
        return asdict(self)


def load_fights() -> list[Fight]:
    if not DATA_FILE.exists():
        return []
    raw = json.loads(DATA_FILE.read_text(encoding="utf-8"))
    return [Fight(**item) for item in raw]


def save_fights(fights: list[Fight]) -> None:
    DATA_FILE.write_text(
        json.dumps([f.to_dict() for f in fights], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def parse_caption(caption: str):
    """Розбирає підпис на (назва, дата_час, місце)."""
    lines = [l.strip() for l in caption.strip().split("\n") if l.strip()]
    if len(lines) < 2:
        return None

    title = lines[0]
    date_line = lines[1]
    location = lines[2] if len(lines) > 2 else ""

    m = re.match(r"(\d{1,2})\.(\d{1,2})\.(\d{4})\s+(\d{1,2}):(\d{2})", date_line)
    if not m:
        return None

    day, month, year, hour, minute = map(int, m.groups())
    try:
        dt = datetime(year, month, day, hour, minute)
    except ValueError:
        return None

    return title, dt, location


def format_countdown(event_dt: datetime) -> str:
    now = datetime.now()
    diff = event_dt - now
    if diff.total_seconds() <= 0:
        return "🔴 Бій почався!"

    days = diff.days
    hours, rem = divmod(diff.seconds, 3600)
    minutes = rem // 60

    parts = []
    if days:
        parts.append(f"{days} дн.")
    parts.append(f"{hours:02d} год.")
    parts.append(f"{minutes:02d} хв.")
    return "⏳ До бою залишилось: " + " ".join(parts)


def build_caption(fight: Fight) -> str:
    event_dt = datetime.fromtimestamp(fight.event_ts)
    lines = [fight.title]
    if fight.location:
        lines.append(f"📍 {fight.location}")
    lines.append(f"🗓 {event_dt.strftime('%d.%m.%Y')} о {event_dt.strftime('%H:%M')}")
    lines.append("")
    lines.append(format_countdown(event_dt))
    return "\n".join(lines)


@dp.message(Command("start"))
async def cmd_start(message: Message):
    await message.answer(
        "Привіт! Надішли мені фото афіші з підписом у форматі:\n\n"
        "Михайло Діденко vs Данило Коваленко\n"
        "21.11.2026 20:00\n"
        "Львів, СК «Грація»\n\n"
        "Я опублікую це в каналі й сам буду оновлювати таймер до бою."
    )


@dp.message(F.photo)
async def handle_photo(message: Message):
    if ADMIN_IDS and message.from_user.id not in ADMIN_IDS:
        await message.answer("Вибачте, постити анонси можете не ви.")
        return

    caption = message.caption or ""
    parsed = parse_caption(caption)
    if not parsed:
        await message.answer(
            "Не зміг розпізнати підпис. Формат:\n\n"
            "Назва бою\n"
            "ДД.ММ.РРРР ГГ:ХХ\n"
            "Місце (необов'язково)"
        )
        return

    title, event_dt, location = parsed
    photo_file_id = message.photo[-1].file_id

    fight = Fight(
        chat_id=CHANNEL_ID,
        message_id=0,
        title=title,
        location=location,
        event_ts=event_dt.timestamp(),
    )

    sent = await bot.send_photo(
        chat_id=CHANNEL_ID,
        photo=photo_file_id,
        caption=build_caption(fight),
    )
    fight.message_id = sent.message_id

    fights = load_fights()
    fights.append(fight)
    save_fights(fights)

    await message.answer("Опубліковано в каналі ✅ Таймер буде оновлюватись автоматично.")


def next_update_delay(event_dt: datetime) -> int:
    """Чим ближче бій, тим частіше оновлюємо підпис (у секундах)."""
    remaining = (event_dt - datetime.now()).total_seconds()
    if remaining <= 0:
        return 3600
    if remaining < 2 * 3600:        # менше 2 год — раз на 5 хв
        return 5 * 60
    if remaining < 24 * 3600:       # менше доби — раз на 30 хв
        return 30 * 60
    return 3 * 3600                 # інакше — раз на 3 год


async def updater_loop():
    while True:
        fights = load_fights()
        changed = False
        soonest_delay = 3 * 3600

        for fight in fights:
            if fight.finished:
                continue
            event_dt = datetime.fromtimestamp(fight.event_ts)
            new_caption = build_caption(fight)
            try:
                await bot.edit_message_caption(
                    chat_id=fight.chat_id,
                    message_id=fight.message_id,
                    caption=new_caption,
                )
            except TelegramBadRequest as e:
                if "message is not modified" not in str(e):
                    log.warning("Не вдалось оновити пост %s: %s", fight.message_id, e)

            if (event_dt - datetime.now()).total_seconds() <= 0:
                fight.finished = True
                changed = True
            else:
                soonest_delay = min(soonest_delay, next_update_delay(event_dt))
            changed = True

        if changed:
            save_fights(fights)

        await asyncio.sleep(soonest_delay)


async def main():
    asyncio.create_task(updater_loop())
    log.info("Бот запущений")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
