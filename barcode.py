"""Чтение штрихкода с фото и поиск товара в Open Food Facts."""
import io
import logging
from dataclasses import dataclass

import httpx
from PIL import Image
from pyzbar.pyzbar import decode as zbar_decode

log = logging.getLogger(__name__)

OFF_URL = "https://world.openfoodfacts.org/api/v2/product/{code}.json"
OFF_FIELDS = "product_name,brands,nutriments"
# Только штрихкоды упаковок с едой — не QR-коды и не Code128 с других этикеток.
BARCODE_TYPES = {"EAN13", "EAN8", "UPCA", "UPCE"}


@dataclass
class Product:
    barcode: str
    name: str
    kcal_100g: float
    protein_100g: float
    fat_100g: float
    carbs_100g: float


def decode_barcodes(image: bytes) -> list[str]:
    """Возвращает штрихкоды, найденные на фото (пустой список — если ничего)."""
    try:
        img = Image.open(io.BytesIO(image))
        img.load()
    except Exception:
        return []
    return [
        c.data.decode("utf-8", "ignore")
        for c in zbar_decode(img)
        if c.type in BARCODE_TYPES
    ]


async def lookup_product(barcode: str) -> Product | None:
    """Ищет товар в Open Food Facts. None — не найден или нет данных о калориях."""
    url = OFF_URL.format(code=barcode)
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(15.0)) as client:
            response = await client.get(url, params={"fields": OFF_FIELDS})
    except httpx.HTTPError as exc:
        log.warning("Open Food Facts недоступен: %s", exc)
        return None

    if response.status_code != 200:
        return None

    data = response.json()
    if data.get("status") != 1:
        return None

    product = data.get("product") or {}
    nutriments = product.get("nutriments") or {}
    kcal = nutriments.get("energy-kcal_100g")
    if kcal is None:
        return None

    name = str(product.get("product_name") or "").strip()
    brand = str(product.get("brands") or "").strip()
    full_name = f"{brand} {name}".strip() or f"Товар {barcode}"

    def num(key: str) -> float:
        try:
            return max(0.0, float(nutriments.get(key) or 0))
        except (TypeError, ValueError):
            return 0.0

    return Product(
        barcode=barcode,
        name=full_name,
        kcal_100g=max(0.0, float(kcal)),
        protein_100g=num("proteins_100g"),
        fat_100g=num("fat_100g"),
        carbs_100g=num("carbohydrates_100g"),
    )
