"""Загрузка настроек из окружения / .env."""
import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()


# Кому разрешён бот, если ALLOWED_USER_IDS не задан в окружении.
DEFAULT_ALLOWED_IDS = "349106724,1217167250,631012438"


def _hour(raw: str, default: int) -> int:
    """Час 0-23 из окружения; мусор и выход за диапазон → значение по умолчанию."""
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return value if 0 <= value <= 23 else default


def _user_ids(raw: str) -> set[int]:
    """Разбирает "123, 456" в множество ID; мусорные значения пропускает."""
    ids: set[int] = set()
    for chunk in raw.replace(";", ",").replace(" ", ",").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            ids.add(int(chunk))
        except ValueError:
            continue
    return ids


@dataclass(frozen=True)
class Settings:
    bot_token: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    openrouter_key: str = os.getenv("OPENROUTER_API_KEY", "")
    model: str = os.getenv("OPENROUTER_MODEL", "google/gemini-2.5-flash")
    timezone: str = os.getenv("TIMEZONE", "Europe/Moscow")
    db_path: str = os.getenv("DB_PATH", "food.db")
    app_url: str = os.getenv("OPENROUTER_APP_URL", "")
    app_name: str = os.getenv("OPENROUTER_APP_NAME", "Calorie Bot")
    # HTTP(S)/SOCKS5-прокси для обхода геоблокировки OpenRouter, напр.
    # http://user:pass@host:port. Пусто — соединяться напрямую.
    proxy_url: str = os.getenv("PROXY_URL", "")
    # Во сколько по локальному времени слать воскресный итог недели.
    report_hour: int = _hour(os.getenv("WEEKLY_REPORT_HOUR", ""), 20)
    # Пустая строка в окружении = доступ всем (явное отключение белого списка).
    allowed_users: set[int] = field(
        default_factory=lambda: _user_ids(
            os.getenv("ALLOWED_USER_IDS", DEFAULT_ALLOWED_IDS)
        )
    )

    def validate(self) -> None:
        missing = [
            name
            for name, value in (
                ("TELEGRAM_BOT_TOKEN", self.bot_token),
                ("OPENROUTER_API_KEY", self.openrouter_key),
            )
            if not value
        ]
        if missing:
            raise SystemExit(
                "Не заданы переменные окружения: "
                + ", ".join(missing)
                + ".\nСкопируйте .env.example в .env и заполните значения."
            )


settings = Settings()
