import html
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo


def eligible_deals(payload: Mapping[str, Any], minimum_discount: int = 20) -> list[dict[str, Any]]:
    """Вернуть популярные платные игры со скидкой, в том числе временно бесплатные."""
    categories = payload.get("top_sellers") or {}
    popular_items = categories.get("items", []) if isinstance(categories, Mapping) else []
    popular_ids = {
        str(item.get("id"))
        for item in popular_items
        if isinstance(item, Mapping) and item.get("id") is not None
    }

    specials = payload.get("specials") or {}
    special_items = specials.get("items", []) if isinstance(specials, Mapping) else []
    deals = []
    for item in special_items:
        if not isinstance(item, Mapping):
            continue
        app_id = item.get("id")
        discount = item.get("discount_percent", 0)
        original_price = item.get("original_price", 0)
        final_price = item.get("final_price", 0)
        if (
            app_id is None
            or not str(app_id).isdigit()
            or str(app_id) not in popular_ids
            or not isinstance(discount, (int, float))
            or discount < minimum_discount
            or not isinstance(original_price, (int, float))
            or original_price <= 0
            or not isinstance(final_price, (int, float))
            or final_price < 0
        ):
            continue
        deals.append(dict(item))
    return deals


def render_deal(item: Mapping[str, Any]) -> str:
    app_id = html.escape(str(item["id"]), quote=True)
    name = html.escape(str(item.get("name") or "Игра"))
    discount = int(item.get("discount_percent", 0))
    currency = str(item.get("currency") or "")
    original_price = item.get("original_price", 0) / 100
    final_price = item.get("final_price", 0) / 100
    if currency == "RUB":
        original = f"{original_price:,.0f} ₽".replace(",", " ")
        final = "Бесплатно" if final_price == 0 else f"{final_price:,.0f} ₽".replace(",", " ")
    else:
        original = f"{original_price:.2f} {html.escape(currency)}"
        final = "Бесплатно" if final_price == 0 else f"{final_price:.2f} {html.escape(currency)}"

    offer = "Можно забрать навсегда" if final_price == 0 else f"Скидка {discount}%"
    message = (
        f'🎮 <a href="https://store.steampowered.com/app/{app_id}/">{name}</a>\n'
        f"{offer}\n"
        f"<s>{original}</s> → <b>{final}</b>"
    )
    expiration = item.get("discount_expiration")
    if isinstance(expiration, (int, float)) and expiration > 0:
        expiration_date = datetime.fromtimestamp(
            expiration, tz=timezone.utc
        ).astimezone(ZoneInfo("Europe/Moscow"))
        message += f"\nАкция до: {expiration_date:%d.%m.%Y, %H:%M} МСК"
    return message


def deal_image(item: Mapping[str, Any]) -> str | None:
    image = item.get("large_capsule_image") or item.get("header_image")
    if isinstance(image, str) and image.startswith("https://"):
        return image
    return None
