from html.parser import HTMLParser
import html
import json
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo


class _FreeGameParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.games: list[dict[str, Any]] = []
        self._current: dict[str, Any] | None = None
        self._capture_title = False
        self._current_text = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if (
            tag == "a"
            and "search_result_row" in (attributes.get("class") or "").split()
            and (app_id := attributes.get("data-ds-appid"))
            and app_id.isdigit()
        ):
            try:
                tag_ids = json.loads(attributes.get("data-ds-tagids", "[]"))
            except json.JSONDecodeError:
                tag_ids = []
            self._current = {
                "id": int(app_id),
                "name": "",
                "_is_free_to_play": isinstance(tag_ids, list) and 113 in tag_ids,
            }
            self._current_text = ""
        elif self._current is not None:
            if tag == "span" and "title" in (attributes.get("class") or "").split():
                self._capture_title = True
            elif tag == "img" and attributes.get("src"):
                self._current["large_capsule_image"] = attributes["src"]

    def handle_data(self, data: str) -> None:
        if self._current is not None:
            self._current_text += data
            if self._capture_title:
                self._current["name"] += data

    def handle_endtag(self, tag: str) -> None:
        if tag == "span":
            self._capture_title = False
        elif tag == "a" and self._current is not None:
            if (
                self._current["_is_free_to_play"]
                and "Бесплатно" in self._current_text
                and self._current["name"].strip()
            ):
                self._current["name"] = self._current["name"].strip()
                self._current["final_price"] = 0
                self._current["original_price"] = 0
                self._current["discount_percent"] = 0
                self._current["currency"] = "RUB"
                self._current["is_free_to_play"] = True
                del self._current["_is_free_to_play"]
                self.games.append(self._current)
            self._current = None
            self._capture_title = False
            self._current_text = ""


def parse_free_games(results_html: str) -> list[dict[str, Any]]:
    parser = _FreeGameParser()
    parser.feed(results_html)
    return parser.games


def active_special_ids(payload: Mapping[str, Any]) -> set[str] | None:
    specials = payload.get("specials")
    if not isinstance(specials, Mapping) or not isinstance(specials.get("items"), list):
        return None
    active_ids = set()
    for category_name in ("specials", "top_sellers", "new_releases", "daily_deal"):
        category = payload.get(category_name)
        items = category.get("items", []) if isinstance(category, Mapping) else []
        active_ids.update(
            str(item["id"])
            for item in items
            if isinstance(item, Mapping)
            and item.get("id") is not None
            and str(item["id"]).isdigit()
            and item.get("discounted") is True
        )
    return active_ids


def eligible_deals(payload: Mapping[str, Any], minimum_discount: int = 20) -> list[dict[str, Any]]:
    """Вернуть подходящие скидки с приоритетом популярных подборок Steam."""
    category_order = ("top_sellers", "new_releases", "daily_deal")
    popularity: dict[str, int] = {}
    candidates: dict[str, dict[str, Any]] = {}

    for category_rank, category_name in enumerate(category_order):
        category = payload.get(category_name)
        items = category.get("items", []) if isinstance(category, Mapping) else []
        for item_rank, item in enumerate(items):
            if not isinstance(item, Mapping) or item.get("type", 0) != 0:
                continue
            app_id = item.get("id")
            if app_id is None:
                continue
            app_id = str(app_id)
            popularity.setdefault(app_id, category_rank * 1000 + item_rank)
            if item.get("discounted") is True:
                candidates[app_id] = dict(item)

    specials = payload.get("specials")
    special_items = specials.get("items", []) if isinstance(specials, Mapping) else []
    for item in special_items:
        if not isinstance(item, Mapping):
            continue
        app_id = item.get("id")
        if app_id is not None and str(app_id) in popularity:
            candidates[str(app_id)] = dict(item)

    deals = []
    for app_id, item in candidates.items():
        discount = item.get("discount_percent", 0)
        original_price = item.get("original_price", 0)
        final_price = item.get("final_price", 0)
        if (
            not app_id.isdigit()
            or item.get("discounted") is not True
            or not isinstance(discount, (int, float))
            or discount < minimum_discount
            or not isinstance(original_price, (int, float))
            or original_price <= 0
            or not isinstance(final_price, (int, float))
            or final_price < 0
        ):
            continue
        item["popularity_rank"] = popularity[app_id]
        deals.append(item)
    return sorted(
        deals,
        key=lambda item: (
            item["popularity_rank"],
            -item["discount_percent"],
            str(item["id"]),
        ),
    )


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

    if item.get("is_free_to_play"):
        return f"🆓 <b>{name}</b>\nПопулярная бесплатная игра в Steam"

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
