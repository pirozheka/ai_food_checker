"""Telegram-бот: оценка калорийности еды по фото и описанию через OpenRouter."""
import asyncio
import logging
import re
from datetime import date, datetime, timedelta
from io import BytesIO
from time import monotonic
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatAction, ParseMode
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.exceptions import TelegramNetworkError, TelegramUnauthorizedError
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from aiogram.utils.markdown import hbold, hcode

import barcode
import storage
from config import settings
from openrouter import Estimate, Item, OpenRouterError, estimate, weekly_advice

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
log = logging.getLogger("calorie-bot")

router = Router()

# Пользователи с запросом в работе — чтобы альбом из 5 фото не улетел разом.
_busy: set[int] = set()

# Штрихкод найден, ждём от пользователя вес порции. (product, время находки)
_pending_barcode: dict[int, tuple[barcode.Product, float, date | None]] = {}
_BARCODE_TTL = 600  # секунд на ответ с весом, потом штрихкод считается неактуальным
_WEIGHT_RE = re.compile(r"(\d+(?:[.,]\d+)?)")

CONFIDENCE = {
    "high": "🟢 высокая",
    "medium": "🟡 средняя",
    "low": "🔴 низкая",
}

# Насколько глубоко в прошлое разрешено дописывать блюда.
_MAX_BACKFILL_DAYS = 30

_RELATIVE_DAYS = {"сегодня": 0, "вчера": 1, "позавчера": 2}
_WEEKDAYS = {
    "пн": 0, "понедельник": 0,
    "вт": 1, "вторник": 1,
    "ср": 2, "среда": 2, "среду": 2,
    "чт": 3, "четверг": 3,
    "пт": 4, "пятница": 4, "пятницу": 4,
    "сб": 5, "суббота": 5, "субботу": 5,
    "вс": 6, "воскресенье": 6,
}
_WEEKDAY_SHORT = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
_MONTHS_GEN = [
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
]


def _parse_day(token: str) -> date | None:
    """«вчера», «пн», «5.09», «2026-09-05» → дата. None, если не распознали."""
    text = token.strip().lower().replace("ё", "е").rstrip(",.:;")
    if not text:
        return None
    today = storage.today_local()

    if text in _RELATIVE_DAYS:
        return today - timedelta(days=_RELATIVE_DAYS[text])
    if text in _WEEKDAYS:
        # Ближайший такой день в прошлом; сегодняшний день недели — это сегодня.
        return today - timedelta(days=(today.weekday() - _WEEKDAYS[text]) % 7)

    # Месяц обязан быть двузначным: «5.09» — дата, а «1.5» в «1.5 кг» — вес.
    if not re.fullmatch(r"\d{1,2}\.\d{2}(?:\.\d{2}|\.\d{4})?|\d{4}-\d{2}-\d{2}", text):
        return None

    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d.%m.%y", "%d.%m"):
        try:
            parsed = datetime.strptime(text, fmt).date()
        except ValueError:
            continue
        if fmt == "%d.%m":
            # Год не указан: берём текущий, а если дата ещё не наступила — прошлый.
            parsed = parsed.replace(year=today.year)
            if parsed > today:
                parsed = parsed.replace(year=today.year - 1)
        return parsed
    return None


def _split_day(text: str) -> tuple[date | None, str]:
    """Отрезает от текста ведущее указание дня: «вчера борщ» → (вчера, «борщ»)."""
    parts = text.strip().split(maxsplit=1)
    if not parts:
        return None, ""
    day = _parse_day(parts[0])
    if day is None:
        return None, text.strip()
    return day, (parts[1].strip() if len(parts) > 1 else "")


def _check_day(day: date) -> str | None:
    """Текст ошибки, если в такой день записывать нельзя."""
    today = storage.today_local()
    if day > today:
        return "В будущее записывать не умею — укажите сегодняшний или прошедший день."
    if (today - day).days > _MAX_BACKFILL_DAYS:
        return f"Слишком давно: дописывать можно не глубже {_MAX_BACKFILL_DAYS} дней назад."
    return None


def _fmt_day(day: date) -> str:
    """«8 сентября, пн»; для сегодня и вчера — словами."""
    today = storage.today_local()
    if day == today:
        return "сегодня"
    if day == today - timedelta(days=1):
        return "вчера"
    return f"{day.day} {_MONTHS_GEN[day.month - 1]}, {_WEEKDAY_SHORT[day.weekday()].lower()}"

HELP = (
    f"{hbold('Что я умею')}\n"
    "Оцениваю калорийность и БЖУ еды по фото и/или описанию.\n\n"
    f"{hbold('Как пользоваться')}\n"
    "📷 Пришлите фото блюда — можно с подписью («порция ~300 г», «без масла»).\n"
    "📦 Или фото штрихкода на упаковке — найду товар и попрошу указать вес.\n"
    "✍️ Или опишите текстом: «овсянка на молоке 250 г и банан».\n"
    "Чем точнее вес и состав — тем точнее оценка.\n\n"
    f"{hbold('Забыли записать вовремя')}\n"
    "Допишите задним числом: <code>/add вчера борщ 300 г</code>, "
    "<code>/add пн овсянка</code>, <code>/add 5.09 кофе с молоком</code>.\n"
    "С фото так же — начните подпись с дня: «вчера, порция 300 г».\n\n"
    f"{hbold('Команды')}\n"
    "/today — итог за сегодня\n"
    "/week — итоги недели с разбором (/week прошлая — предыдущая)\n"
    "/add — дописать блюдо за прошлый день\n"
    "/day — дневник за конкретный день\n"
    "/undo — удалить последнюю запись\n"
    "/clear — очистить сегодняшний день\n"
    "/help — эта справка\n\n"
    "📬 В воскресенье вечером пришлю итоги недели сам.\n"
    "⚠️ Оценка приблизительная и не является медицинской рекомендацией."
)


def _allowed(user_id: int | None, chat_id: int | None) -> bool:
    """Пускаем, если белый список пуст либо в нём есть ID пользователя или чата."""
    if not settings.allowed_users:
        return True
    return bool(
        {i for i in (user_id, chat_id) if i is not None} & settings.allowed_users
    )


def _fmt_estimate(est: Estimate, day: date | None = None) -> str:
    t = est.total
    lines = [f"🍽 {hbold(est.dish)}"]
    if day is not None and day != storage.today_local():
        lines.append(f"📅 записано на {_fmt_day(day)}")
    if t.weight_g:
        lines.append(f"Вес: ~{t.weight_g:.0f} г")
    lines.append("")
    lines.append(f"🔥 {hbold(f'{t.kcal:.0f} ккал')}")
    lines.append(
        f"Б {t.protein_g:.1f} г · Ж {t.fat_g:.1f} г · У {t.carbs_g:.1f} г"
    )

    if len(est.items) > 1:
        lines.append("")
        lines.append(hbold("Состав"))
        for item in est.items:
            weight = f" {item.weight_g:.0f} г —" if item.weight_g else " —"
            lines.append(f"• {item.name}:{weight} {item.kcal:.0f} ккал")

    lines.append("")
    lines.append(f"Точность: {CONFIDENCE.get(est.confidence, est.confidence)}")
    if est.assumptions:
        lines.append(f"📌 {est.assumptions}")
    if est.note:
        lines.append(f"❓ {est.note}")
    return "\n".join(lines)


def _keyboard(meal_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🗑 Убрать из дневника", callback_data=f"del:{meal_id}"),
                InlineKeyboardButton(text="📊 За сегодня", callback_data="today"),
            ]
        ]
    )


async def _day_text(user_id: int, day: date | None = None) -> str:
    target = day or storage.today_local()
    totals, rows = await storage.day(user_id, target)
    title = "Сегодня" if target == storage.today_local() else _fmt_day(target).capitalize()
    if not rows:
        if target == storage.today_local():
            return "За сегодня пока ничего не записано. Пришлите фото или описание еды."
        return f"За {_fmt_day(target)} записей нет."
    lines = [hbold(title)]
    for row in rows:
        lines.append(f"• {row['dish']} — {row['kcal']:.0f} ккал")
    lines.append("")
    lines.append(f"🔥 Итого: {hbold(f'{totals.kcal:.0f} ккал')} за {totals.count} приём(ов)")
    lines.append(
        f"Б {totals.protein_g:.1f} г · Ж {totals.fat_g:.1f} г · У {totals.carbs_g:.1f} г"
    )
    return "\n".join(lines)


def _macro_split(totals: storage.DayTotals) -> str:
    """Доли БЖУ по калориям: белок и углеводы по 4 ккал/г, жир — 9."""
    p, f, c = totals.protein_g * 4, totals.fat_g * 9, totals.carbs_g * 4
    energy = p + f + c
    if energy <= 0:
        return ""
    return f"Баланс: Б {p / energy:.0%} · Ж {f / energy:.0%} · У {c / energy:.0%}"


async def _week_text(user_id: int, monday: date, with_advice: bool = True) -> str:
    report = await storage.week(user_id, monday)
    period = f"{report.monday.day} {_MONTHS_GEN[report.monday.month - 1]} — " \
             f"{report.sunday.day} {_MONTHS_GEN[report.sunday.month - 1]}"
    if not report.totals.count:
        return f"📅 {hbold('Неделя ' + period)}\n\nЗа эту неделю записей нет."

    lines = [f"📅 {hbold('Итоги недели')}: {period}", ""]
    for day_date, totals in report.days:
        name = _WEEKDAY_SHORT[day_date.weekday()]
        if totals.count:
            lines.append(f"{name}  {totals.kcal:.0f} ккал  ({totals.count})")
        else:
            lines.append(f"{name}  —")

    lines.append("")
    lines.append(f"🔥 Всего: {hbold(f'{report.totals.kcal:.0f} ккал')}")
    lines.append(
        f"В среднем: {hbold(f'{report.avg_kcal:.0f} ккал/день')} "
        f"({report.active_days} дн. с записями)"
    )
    lines.append(
        f"Б {report.totals.protein_g:.0f} г · Ж {report.totals.fat_g:.0f} г · "
        f"У {report.totals.carbs_g:.0f} г"
    )
    if split := _macro_split(report.totals):
        lines.append(split)

    if with_advice:
        advice = await weekly_advice(_week_stats_for_model(report, await storage.week_dishes(user_id, monday)))
        if advice:
            lines.append("")
            lines.append(f"💡 {hbold('Коротко')}")
            lines.append(advice)

    lines.append("")
    lines.append("⚠️ Оценка приблизительная и не является медицинской рекомендацией.")
    return "\n".join(lines)


def _week_stats_for_model(report: storage.WeekReport, dishes: list[str]) -> str:
    """Сухая сводка недели для модели — на её основе она пишет рекомендации."""
    by_day = "\n".join(
        f"{_WEEKDAY_SHORT[d.weekday()]}: "
        + (f"{t.kcal:.0f} ккал, Б {t.protein_g:.0f} Ж {t.fat_g:.0f} У {t.carbs_g:.0f}"
           if t.count else "нет записей")
        for d, t in report.days
    )
    return (
        f"Калории по дням:\n{by_day}\n\n"
        f"Всего за неделю: {report.totals.kcal:.0f} ккал за {report.active_days} дн. с записями.\n"
        f"Среднее: {report.avg_kcal:.0f} ккал/день.\n"
        f"БЖУ за неделю: белки {report.totals.protein_g:.0f} г, "
        f"жиры {report.totals.fat_g:.0f} г, углеводы {report.totals.carbs_g:.0f} г.\n"
        f"Что ел: {', '.join(dishes[:25]) if dishes else 'нет данных'}."
    )


def _parse_weight(text: str) -> float | None:
    """Достаёт число (г/мл) из текста; «1.5 кг» → 1500."""
    match = _WEIGHT_RE.search(text.replace(",", "."))
    if not match:
        return None
    value = float(match.group(1))
    if re.search(r"\bкг\b|\bkg\b", text, re.IGNORECASE):
        value *= 1000
    return value if value > 0 else None


async def _apply_barcode_weight(
    message: Message, product: barcode.Product, weight_g: float, day: date | None = None
) -> None:
    if weight_g > 5000:
        await message.reply("Похоже на ошибку в весе. Укажите вес от 1 до 5000 г/мл.")
        return

    factor = weight_g / 100.0
    total = Item(
        name=product.name,
        weight_g=weight_g,
        kcal=product.kcal_100g * factor,
        protein_g=product.protein_100g * factor,
        fat_g=product.fat_100g * factor,
        carbs_g=product.carbs_100g * factor,
    )
    est = Estimate(
        is_food=True,
        dish=product.name,
        confidence="high",
        total=total,
        items=[],
        assumptions=f"Данные Open Food Facts по штрихкоду {product.barcode}.",
        note="",
    )
    meal_id = await storage.add_meal(
        message.from_user.id,
        est.dish,
        total.weight_g,
        total.kcal,
        total.protein_g,
        total.fat_g,
        total.carbs_g,
        day=day,
    )
    await message.reply(_fmt_estimate(est, day), reply_markup=_keyboard(meal_id))


async def _process_photo(
    message: Message, image: bytes, caption: str, day: date | None = None
) -> None:
    """Сначала пробует прочитать штрихкод, иначе — обычная фото-оценка."""
    user_id = message.from_user.id
    _pending_barcode.pop(user_id, None)  # новое фото отменяет прошлый незавершённый штрихкод

    for code in barcode.decode_barcodes(image):
        product = await barcode.lookup_product(code)
        if product:
            _pending_barcode[user_id] = (product, monotonic(), day)
            for_day = f" за {_fmt_day(day)}" if day and day != storage.today_local() else ""
            await message.reply(
                f"📦 {hbold(product.name)}\n"
                f"{product.kcal_100g:.0f} ккал / 100 г "
                f"(Б {product.protein_100g:.1f} · Ж {product.fat_100g:.1f} · "
                f"У {product.carbs_100g:.1f})\n\n"
                f"Сколько грамм или мл вы съели/выпили{for_day}? Пришлите число, "
                "например «150» или «1.5 кг»."
            )
            return

    text = (
        f"Оцени калорийность блюда на фото. Уточнение от пользователя: {caption}"
        if caption
        else "Оцени калорийность блюда на фото."
    )
    await _handle_meal(message, text, image, day)


async def _handle_meal(
    message: Message, text: str, image: bytes | None, day: date | None = None
) -> None:
    user_id = message.from_user.id
    if user_id in _busy:
        await message.reply("Секунду, ещё считаю предыдущее блюдо 🙏")
        return

    # Флаг снимаем в finally, иначе сетевая ошибка ниже навсегда пометит
    # пользователя занятым и все следующие блюда получат «Секунду…».
    _busy.add(user_id)
    try:
        thinking = await message.reply("🔍 Анализирую…")
        try:
            await message.bot.send_chat_action(message.chat.id, ChatAction.TYPING)
            est = await estimate(text=text, image=image)
        except OpenRouterError as exc:
            await thinking.edit_text(f"⚠️ {exc}")
            return
        except Exception:  # noqa: BLE001 — не роняем бота из-за одного сообщения
            log.exception("Ошибка при оценке блюда")
            await thinking.edit_text("⚠️ Что-то пошло не так. Попробуйте ещё раз.")
            return

        if not est.is_food or est.total.kcal <= 0:
            note = est.note or "Не вижу здесь еды. Пришлите фото блюда или опишите его текстом."
            await thinking.edit_text(f"🤔 {note}")
            return

        meal_id = await storage.add_meal(
            user_id,
            est.dish,
            est.total.weight_g,
            est.total.kcal,
            est.total.protein_g,
            est.total.fat_g,
            est.total.carbs_g,
            day=day,
        )
        result = _fmt_estimate(est, day)
        try:
            await thinking.edit_text(result, reply_markup=_keyboard(meal_id))
        except TelegramNetworkError:
            # Блюдо уже в дневнике — сообщаем отдельно, чтобы не осталось «Анализирую…».
            log.warning("Не удалось отредактировать сообщение с результатом", exc_info=True)
            await message.answer(result, reply_markup=_keyboard(meal_id))
    finally:
        _busy.discard(user_id)


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    await message.answer(
        f"Привет! Я считаю калории по фото еды 🍎\n\n{HELP}"
    )


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(HELP)


@router.message(Command("today"))
async def cmd_today(message: Message) -> None:
    await message.answer(await _day_text(message.from_user.id))


@router.message(Command("undo"))
async def cmd_undo(message: Message) -> None:
    dish = await storage.delete_last(message.from_user.id)
    await message.answer(
        f"Удалил запись: {dish}" if dish else "Нечего удалять — дневник пуст."
    )


@router.message(Command("clear"))
async def cmd_clear(message: Message) -> None:
    removed = await storage.clear_today(message.from_user.id)
    await message.answer(f"Очистил сегодняшний день: удалено записей — {removed}.")


_ADD_HELP = (
    f"{hbold('Дописать блюдо задним числом')}\n"
    "<code>/add вчера борщ 300 г и хлеб</code>\n"
    "<code>/add пн овсянка на молоке</code>\n"
    "<code>/add 5.09 два яйца и кофе с молоком</code>\n\n"
    "День недели (пн…вс) — это ближайший такой день в прошлом. "
    "Фото тоже можно: начните подпись с дня — «вчера, порция 300 г»."
)


@router.message(Command("add"))
async def cmd_add(message: Message, command: CommandObject) -> None:
    if not command.args:
        await message.answer(_ADD_HELP)
        return

    day, description = _split_day(command.args)
    if day is None:
        await message.answer(
            "Не понял, за какой день записать.\n\n" + _ADD_HELP
        )
        return
    if problem := _check_day(day):
        await message.answer(problem)
        return
    if len(description) < 2:
        await message.answer("После дня опишите блюдо: <code>/add вчера гречка с курицей</code>")
        return

    await _handle_meal(
        message,
        f"Оцени калорийность по описанию пользователя: {description}",
        None,
        day,
    )


@router.message(Command("day"))
async def cmd_day(message: Message, command: CommandObject) -> None:
    if not command.args:
        await message.answer(
            "Укажите день: <code>/day вчера</code>, <code>/day пт</code> или "
            "<code>/day 5.09</code>"
        )
        return
    day, _ = _split_day(command.args)
    if day is None:
        await message.answer("Не понял день. Попробуйте «вчера», «пт» или «5.09».")
        return
    await message.answer(await _day_text(message.from_user.id, day))


@router.message(Command("week"))
async def cmd_week(message: Message, command: CommandObject) -> None:
    """Текущая неделя; /week прошлая — предыдущая."""
    today = storage.today_local()
    monday = today - timedelta(days=today.weekday())
    if command.args and command.args.strip().lower() in {"прошлая", "прошлую", "last"}:
        monday -= timedelta(days=7)
    thinking = await message.answer("📊 Собираю итоги недели…")
    text = await _week_text(message.from_user.id, monday)
    try:
        await thinking.edit_text(text)
    except TelegramNetworkError:
        await message.answer(text)


async def _download(message: Message, file) -> bytes | None:
    """Качает файл из Telegram. None + объяснение пользователю, если сеть подвела."""
    buffer = BytesIO()
    try:
        await message.bot.download(file, destination=buffer)
    except TelegramNetworkError:
        log.warning("Не удалось скачать файл из Telegram", exc_info=True)
        await message.reply("Не смог забрать фото у Telegram 😕 Пришлите его ещё раз.")
        return None
    return buffer.getvalue()


async def _caption_day(message: Message) -> tuple[date | None, str] | None:
    """Подпись к фото: ведущее «вчера»/«пн»/«5.09» задаёт день записи."""
    day, caption = _split_day((message.caption or "").strip())
    if day is not None and (problem := _check_day(day)):
        await message.reply(problem)
        return None
    return day, caption


@router.message(F.photo)
async def on_photo(message: Message) -> None:
    parsed = await _caption_day(message)
    if parsed is None:
        return
    day, caption = parsed
    photo = message.photo[-1]  # максимальное доступное разрешение
    image = await _download(message, photo)
    if image is None:
        return
    await _process_photo(message, image, caption, day)


@router.message(F.text & ~F.text.startswith("/"))
async def on_text(message: Message) -> None:
    user_id = message.from_user.id
    description = message.text.strip()

    pending = _pending_barcode.get(user_id)
    if pending and monotonic() - pending[1] <= _BARCODE_TTL:
        weight = _parse_weight(description)
        if weight is not None:
            del _pending_barcode[user_id]
            await _apply_barcode_weight(message, pending[0], weight, pending[2])
            return
        _pending_barcode.pop(user_id, None)  # похоже, передумали — сбрасываем ожидание
    else:
        _pending_barcode.pop(user_id, None)

    if len(description) < 2:
        await message.reply("Опишите блюдо подробнее или пришлите фото.")
        return
    await _handle_meal(
        message,
        f"Оцени калорийность по описанию пользователя: {description}",
        None,
    )


@router.message(F.document.mime_type.startswith("image/"))
async def on_image_document(message: Message) -> None:
    parsed = await _caption_day(message)
    if parsed is None:
        return
    day, caption = parsed
    image = await _download(message, message.document)
    if image is None:
        return
    await _process_photo(message, image, caption, day)


@router.message()
async def on_other(message: Message) -> None:
    await message.reply("Пришлите фото еды 📷 или опишите блюдо текстом ✍️")


@router.callback_query(F.data.startswith("del:"))
async def cb_delete(callback: CallbackQuery) -> None:
    meal_id = int(callback.data.split(":", 1)[1])
    removed = await storage.delete_meal(callback.from_user.id, meal_id)
    await callback.answer("Убрал из дневника" if removed else "Записи уже нет")
    if removed and callback.message:
        await callback.message.edit_reply_markup(reply_markup=None)


@router.callback_query(F.data == "today")
async def cb_today(callback: CallbackQuery) -> None:
    await callback.answer()
    if callback.message:
        await callback.message.answer(await _day_text(callback.from_user.id))


@router.message.outer_middleware()
async def access_control_message(handler, event: Message, data):
    user_id = event.from_user.id if event.from_user else None
    if not _allowed(user_id, event.chat.id):
        log.warning("Отказ в доступе: user_id=%s chat_id=%s", user_id, event.chat.id)
        await event.answer(
            "Извините, бот приватный.\n"
            f"Ваш ID: {hcode(str(user_id))} — попросите владельца добавить его "
            "в ALLOWED_USER_IDS."
        )
        return None
    return await handler(event, data)


@router.callback_query.outer_middleware()
async def access_control_callback(handler, event: CallbackQuery, data):
    chat_id = event.message.chat.id if event.message else None
    if not _allowed(event.from_user.id, chat_id):
        log.warning("Отказ в доступе (callback): user_id=%s", event.from_user.id)
        await event.answer("Бот приватный.", show_alert=True)
        return None
    return await handler(event, data)


async def _send_weekly_reports(bot: Bot) -> None:
    """Воскресным вечером — итог недели каждому, кто на этой неделе что-то записал."""
    now = datetime.now(ZoneInfo(settings.timezone))
    if now.weekday() != 6 or now.hour < settings.report_hour:
        return

    monday = now.date() - timedelta(days=6)
    for user_id in await storage.users_with_meals(monday):
        # Отметку ставим до отправки: пусть лучше отчёт потеряется при сбое,
        # чем пользователь получит его дважды.
        if not await storage.mark_report_sent(user_id, monday):
            continue
        try:
            await bot.send_message(user_id, await _week_text(user_id, monday))
            log.info("Недельный отчёт отправлен: user_id=%s неделя с %s", user_id, monday)
        except Exception:  # noqa: BLE001 — один недоставленный отчёт не должен ронять задачу
            log.exception("Не удалось отправить недельный отчёт user_id=%s", user_id)


async def _weekly_scheduler(bot: Bot) -> None:
    while True:
        try:
            await _send_weekly_reports(bot)
        except Exception:  # noqa: BLE001
            log.exception("Ошибка планировщика недельных отчётов")
        await asyncio.sleep(600)


async def main() -> None:
    settings.validate()
    await storage.init()
    bot = Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher()
    dp.include_router(router)
    if settings.allowed_users:
        log.info(
            "Доступ разрешён только ID: %s",
            ", ".join(str(i) for i in sorted(settings.allowed_users)),
        )
    else:
        log.warning("ALLOWED_USER_IDS пуст — бот открыт всем!")
    log.info("Бот запущен. Модель: %s", settings.model)
    log.info(
        "Недельный отчёт: воскресенье, %02d:00 (%s)", settings.report_hour, settings.timezone
    )
    scheduler = asyncio.create_task(_weekly_scheduler(bot))
    try:
        await bot.set_my_commands(
            [
                BotCommand(command="today", description="Итог за сегодня"),
                BotCommand(command="week", description="Итоги недели"),
                BotCommand(command="add", description="Дописать блюдо за прошлый день"),
                BotCommand(command="day", description="Дневник за конкретный день"),
                BotCommand(command="undo", description="Удалить последнюю запись"),
                BotCommand(command="clear", description="Очистить сегодняшний день"),
                BotCommand(command="help", description="Справка"),
            ]
        )
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot)
    except TelegramUnauthorizedError:
        raise SystemExit("Telegram отклонил токен. Проверьте TELEGRAM_BOT_TOKEN в .env.")
    finally:
        scheduler.cancel()
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit) as exc:
        if isinstance(exc, SystemExit) and exc.code:
            raise
        log.info("Остановлено")
