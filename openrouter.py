"""Клиент OpenRouter: отправка фото/текста в vision-модель и разбор JSON-ответа."""
import asyncio
import base64
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

import httpx

from config import settings
from prompts import RESPONSE_SCHEMA, SYSTEM_PROMPT, WEEKLY_SYSTEM_PROMPT

log = logging.getLogger(__name__)

API_URL = "https://openrouter.ai/api/v1/chat/completions"
JSON_BLOCK = re.compile(r"\{.*\}", re.DOTALL)


class OpenRouterError(RuntimeError):
    """Ошибка запроса к OpenRouter, текст пригоден для показа пользователю."""


@dataclass
class Item:
    name: str
    weight_g: float = 0.0
    kcal: float = 0.0
    protein_g: float = 0.0
    fat_g: float = 0.0
    carbs_g: float = 0.0


@dataclass
class Estimate:
    is_food: bool
    dish: str
    confidence: str
    total: Item
    items: list[Item] = field(default_factory=list)
    assumptions: str = ""
    note: str = ""


def _item(raw: dict[str, Any], default_name: str = "") -> Item:
    def num(key: str) -> float:
        try:
            return max(0.0, float(raw.get(key) or 0))
        except (TypeError, ValueError):
            return 0.0

    return Item(
        name=str(raw.get("name") or default_name).strip(),
        weight_g=num("weight_g"),
        kcal=num("kcal"),
        protein_g=num("protein_g"),
        fat_g=num("fat_g"),
        carbs_g=num("carbs_g"),
    )


def _parse(content: str) -> Estimate:
    text = content.strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text.split("\n", 1)[-1] if text.lower().startswith("json") else text
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        match = JSON_BLOCK.search(content)
        if not match:
            raise OpenRouterError("Модель вернула ответ не в формате JSON.")
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError as exc:
            raise OpenRouterError("Не удалось разобрать ответ модели.") from exc

    dish = str(data.get("dish") or "Блюдо").strip()
    items = [_item(raw) for raw in data.get("items") or [] if isinstance(raw, dict)]
    total_raw = data.get("total")
    if isinstance(total_raw, dict):
        total = _item(total_raw, dish)
    else:  # модель забыла total — считаем сами
        total = Item(
            name=dish,
            weight_g=sum(i.weight_g for i in items),
            kcal=sum(i.kcal for i in items),
            protein_g=sum(i.protein_g for i in items),
            fat_g=sum(i.fat_g for i in items),
            carbs_g=sum(i.carbs_g for i in items),
        )
    total.name = dish

    confidence = str(data.get("confidence") or "medium").lower()
    if confidence not in {"high", "medium", "low"}:
        confidence = "medium"

    return Estimate(
        is_food=bool(data.get("is_food", total.kcal > 0)),
        dish=dish,
        confidence=confidence,
        total=total,
        items=items,
        assumptions=str(data.get("assumptions") or "").strip(),
        note=str(data.get("note") or "").strip(),
    )


def _user_content(text: str, image: bytes | None, mime: str) -> list[dict[str, Any]]:
    content: list[dict[str, Any]] = []
    if image:
        data_uri = f"data:{mime};base64,{base64.b64encode(image).decode()}"
        content.append({"type": "image_url", "image_url": {"url": data_uri}})
    content.append({"type": "text", "text": text})
    return content


async def estimate(
    *,
    text: str,
    image: bytes | None = None,
    mime: str = "image/jpeg",
    model: str | None = None,
) -> Estimate:
    """Спрашивает модель о калорийности блюда. Бросает OpenRouterError."""
    headers = {
        "Authorization": f"Bearer {settings.openrouter_key}",
        "Content-Type": "application/json",
    }
    if settings.app_url:
        headers["HTTP-Referer"] = settings.app_url
    if settings.app_name:
        headers["X-Title"] = settings.app_name

    payload = {
        "model": model or settings.model,
        "temperature": 0.2,
        "max_tokens": 1500,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": _user_content(text, image, mime)},
        ],
        "response_format": {"type": "json_schema", "json_schema": RESPONSE_SCHEMA},
    }

    last_error: Exception | None = None
    schema_dropped = False
    # 45 с × 3 попытки: при недоступном OpenRouter пользователь получит внятную
    # ошибку примерно за две минуты, а не будет полчаса смотреть на «Анализирую…».
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(45.0), proxy=settings.proxy_url or None
    ) as client:
        for attempt in range(3):
            try:
                response = await client.post(API_URL, headers=headers, json=payload)
            except httpx.HTTPError as exc:
                last_error = exc
                log.warning("Сеть недоступна (попытка %s): %s", attempt + 1, exc)
                await asyncio.sleep(2 ** attempt)
                continue

            if response.status_code == 200:
                body = response.json()
                if "error" in body and not body.get("choices"):
                    raise OpenRouterError(str(body["error"].get("message", body["error"])))
                try:
                    content = body["choices"][0]["message"]["content"]
                except (KeyError, IndexError) as exc:
                    raise OpenRouterError("Пустой ответ от модели.") from exc
                if isinstance(content, list):  # некоторые модели возвращают список частей
                    content = "".join(
                        part.get("text", "") for part in content if isinstance(part, dict)
                    )
                return _parse(content or "")

            if response.status_code in (408, 429, 500, 502, 503, 504):
                last_error = OpenRouterError(f"OpenRouter вернул {response.status_code}")
                log.warning("Повтор из-за %s: %s", response.status_code, response.text[:300])
                await asyncio.sleep(2 ** attempt + 1)
                continue

            detail = response.text[:300]
            # Не все модели OpenRouter поддерживают json_schema — повторяем без него.
            if (
                not schema_dropped
                and response.status_code in (400, 404, 422)
                and any(
                    marker in detail.lower()
                    for marker in ("structured", "response_format", "json_schema", "schema")
                )
            ):
                schema_dropped = True
                payload.pop("response_format", None)
                payload["messages"][0]["content"] += (
                    "\n\nВерни ответ строго одним JSON-объектом по описанной схеме."
                )
                log.info("Модель не поддерживает json_schema, повторяю без него")
                continue
            if response.status_code == 401:
                raise OpenRouterError("OpenRouter отклонил ключ (401). Проверьте OPENROUTER_API_KEY.")
            if response.status_code == 402:
                raise OpenRouterError("На балансе OpenRouter недостаточно средств (402).")
            raise OpenRouterError(f"Ошибка OpenRouter {response.status_code}: {detail}")

    raise OpenRouterError(f"OpenRouter недоступен: {last_error}")


async def weekly_advice(stats: str) -> str:
    """Краткий разбор недели. Пустая строка, если модель недоступна —
    отчёт с цифрами важнее рекомендаций и должен уйти в любом случае."""
    headers = {
        "Authorization": f"Bearer {settings.openrouter_key}",
        "Content-Type": "application/json",
    }
    if settings.app_url:
        headers["HTTP-Referer"] = settings.app_url
    if settings.app_name:
        headers["X-Title"] = settings.app_name

    payload = {
        "model": settings.model,
        "temperature": 0.4,
        "max_tokens": 300,
        "messages": [
            {"role": "system", "content": WEEKLY_SYSTEM_PROMPT},
            {"role": "user", "content": stats},
        ],
    }
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(45.0), proxy=settings.proxy_url or None
        ) as client:
            response = await client.post(API_URL, headers=headers, json=payload)
        if response.status_code != 200:
            log.warning("Рекомендации не получены: %s %s", response.status_code, response.text[:200])
            return ""
        content = response.json()["choices"][0]["message"]["content"]
        if isinstance(content, list):
            content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
        return (content or "").strip()
    except (httpx.HTTPError, KeyError, IndexError, ValueError):
        log.warning("Рекомендации не получены", exc_info=True)
        return ""
