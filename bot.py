import argparse
import asyncio
import logging
import os
import sqlite3
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import NetworkError, RetryAfter, TelegramError, TimedOut
from telegram.ext import Application, CommandHandler, ContextTypes

from steam_deals import (
    active_special_ids,
    deal_image,
    eligible_deals,
    parse_free_games,
    render_deal,
)

STEAM_FEATURED_URL = "https://store.steampowered.com/api/featuredcategories/"
STEAM_FREE_GAMES_URL = "https://store.steampowered.com/search/results/"
MOSCOW_TIME = ZoneInfo("Europe/Moscow")
MAX_FREE_GAME_POSTS_PER_DAY = 1
REQUEST_ATTEMPTS = 3
TELEGRAM_ATTEMPTS = 3


class PublicationError(TelegramError):
    pass


@dataclass(frozen=True)
class Config:
    token: str
    channel_id: str
    country_code: str
    language: str
    check_interval_minutes: int
    minimum_discount: int
    max_posts_per_check: int
    database_path: str
    admin_user_ids: frozenset[int]


def load_config() -> Config:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    channel_id = os.environ.get("TELEGRAM_CHANNEL_ID", "").strip()
    if not token or not channel_id:
        raise ValueError("Укажите TELEGRAM_BOT_TOKEN и TELEGRAM_CHANNEL_ID в настройках окружения.")

    interval = int(os.environ.get("CHECK_INTERVAL_MINUTES", "10"))
    minimum_discount = int(os.environ.get("MINIMUM_DISCOUNT_PERCENT", "20"))
    max_posts = int(os.environ.get("MAX_POSTS_PER_CHECK", "5"))
    if interval < 1 or not 0 <= minimum_discount <= 100 or max_posts < 1:
        raise ValueError(
            "Проверьте интервал проверки, минимальный размер скидки и число публикаций."
        )

    admin_ids = frozenset(
        int(value.strip())
        for value in os.environ.get("ADMIN_USER_IDS", "").split(",")
        if value.strip()
    )
    return Config(
        token=token,
        channel_id=channel_id,
        country_code=os.environ.get("STEAM_COUNTRY_CODE", "ru").strip(),
        language=os.environ.get("STEAM_LANGUAGE", "russian").strip(),
        check_interval_minutes=interval,
        minimum_discount=minimum_discount,
        max_posts_per_check=max_posts,
        database_path=os.environ.get("DATABASE_PATH", "steam_deals.sqlite3"),
        admin_user_ids=admin_ids,
    )


def initialize_database(path: str) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS published_deals "
            "(app_id TEXT PRIMARY KEY, published_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, "
            "lowest_final_price INTEGER, last_offer_expiration INTEGER, "
            "offer_active INTEGER NOT NULL DEFAULT 1)"
        )
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(published_deals)")
        }
        if "lowest_final_price" not in columns:
            connection.execute(
                "ALTER TABLE published_deals ADD COLUMN lowest_final_price INTEGER"
            )
        if "last_offer_expiration" not in columns:
            connection.execute(
                "ALTER TABLE published_deals ADD COLUMN last_offer_expiration INTEGER"
            )
        if "offer_active" not in columns:
            connection.execute(
                "ALTER TABLE published_deals ADD COLUMN offer_active "
                "INTEGER NOT NULL DEFAULT 1"
            )
        connection.execute(
            "CREATE TABLE IF NOT EXISTS published_free_games "
            "(app_id TEXT PRIMARY KEY, published_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
        )
        connection.execute(
            "CREATE TABLE IF NOT EXISTS daily_free_game_posts "
            "(post_date TEXT PRIMARY KEY, app_id TEXT NOT NULL)"
        )


def mark_missing_offers_inactive(path: str, active_app_ids: set[str]) -> None:
    with sqlite3.connect(path) as connection:
        if active_app_ids:
            placeholders = ",".join("?" for _ in active_app_ids)
            connection.execute(
                f"UPDATE published_deals SET offer_active = 0 "
                f"WHERE app_id NOT IN ({placeholders})",
                tuple(active_app_ids),
            )
        else:
            connection.execute("UPDATE published_deals SET offer_active = 0")


def should_publish(
    path: str, app_id: str, final_price: int, offer_expiration: int | None
) -> bool:
    with sqlite3.connect(path) as connection:
        result = connection.execute(
            "SELECT lowest_final_price, last_offer_expiration, offer_active "
            "FROM published_deals WHERE app_id = ?",
            (app_id,),
        ).fetchone()
        if result is None:
            return True
        lowest_final_price, last_offer_expiration, offer_active = result
        if not offer_active or (
            offer_expiration is not None
            and last_offer_expiration is not None
            and offer_expiration != last_offer_expiration
        ):
            return True
        if lowest_final_price is None:
            connection.execute(
                "UPDATE published_deals SET lowest_final_price = ?, "
                "last_offer_expiration = ? WHERE app_id = ?",
                (final_price, offer_expiration, app_id),
            )
            return False
        if last_offer_expiration is None and offer_expiration is not None:
            connection.execute(
                "UPDATE published_deals SET last_offer_expiration = ? WHERE app_id = ?",
                (offer_expiration, app_id),
            )
        return final_price < lowest_final_price


def mark_published(
    path: str, app_id: str, final_price: int, offer_expiration: int | None
) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute(
            "INSERT INTO published_deals "
            "(app_id, lowest_final_price, last_offer_expiration, offer_active) "
            "VALUES (?, ?, ?, 1) "
            "ON CONFLICT(app_id) DO UPDATE SET lowest_final_price = "
            "CASE WHEN published_deals.lowest_final_price IS NULL "
            "THEN excluded.lowest_final_price ELSE "
            "MIN(published_deals.lowest_final_price, excluded.lowest_final_price) END, "
            "last_offer_expiration = excluded.last_offer_expiration, offer_active = 1",
            (app_id, final_price, offer_expiration),
        )


def can_publish_free_game(path: str, app_id: str, post_date: str) -> bool:
    with sqlite3.connect(path) as connection:
        already_posted = connection.execute(
            "SELECT 1 FROM published_free_games WHERE app_id = ?", (app_id,)
        ).fetchone()
        if already_posted:
            return False
        daily_count = connection.execute(
            "SELECT COUNT(*) FROM daily_free_game_posts WHERE post_date = ?",
            (post_date,),
        ).fetchone()[0]
    return daily_count < MAX_FREE_GAME_POSTS_PER_DAY


def mark_free_game_published(path: str, app_id: str, post_date: str) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute(
            "INSERT INTO published_free_games (app_id) VALUES (?)", (app_id,)
        )
        connection.execute(
            "INSERT INTO daily_free_game_posts (post_date, app_id) VALUES (?, ?)",
            (post_date, app_id),
        )


async def request_with_retry(
    client: httpx.AsyncClient, url: str, params: dict[str, str]
) -> httpx.Response:
    for attempt in range(REQUEST_ATTEMPTS):
        try:
            response = await client.get(url, params=params)
            if response.status_code == 429 or response.status_code >= 500:
                if attempt + 1 < REQUEST_ATTEMPTS:
                    await asyncio.sleep(2**attempt)
                    continue
            response.raise_for_status()
            return response
        except httpx.RequestError:
            if attempt + 1 == REQUEST_ATTEMPTS:
                raise
            await asyncio.sleep(2**attempt)
    raise httpx.HTTPError("Steam не ответил после нескольких попыток.")


async def fetch_deals(
    config: Config,
) -> tuple[list[dict], set[str] | None, list[dict]]:
    async with httpx.AsyncClient(
        timeout=20,
        headers={"User-Agent": "Mozilla/5.0 SteamDealsTelegramBot/1.0"},
    ) as client:
        response = await request_with_retry(
            client,
            STEAM_FEATURED_URL,
            params={"cc": config.country_code, "l": config.language},
        )
        payload = response.json()
        free_response = await request_with_retry(
            client,
            STEAM_FREE_GAMES_URL,
            params={
                "query": "",
                "start": "0",
                "count": "50",
                "dynamic_data": "",
                "sort_by": "Reviews_DESC",
                "filter": "free",
                "infinite": "1",
                "cc": config.country_code,
                "l": config.language,
            },
        )
        free_payload = free_response.json()
    if not isinstance(payload, dict):
        raise ValueError("Steam вернул подборки игр в неожиданном формате.")
    if not isinstance(free_payload, dict) or not isinstance(
        free_payload.get("results_html"), str
    ):
        raise ValueError("Steam вернул список бесплатных игр в неожиданном формате.")
    return (
        eligible_deals(payload, config.minimum_discount),
        active_special_ids(payload),
        parse_free_games(free_payload["results_html"]),
    )


async def send_with_retry(
    send: Callable[[], Awaitable[object]],
) -> object:
    for attempt in range(TELEGRAM_ATTEMPTS):
        try:
            return await send()
        except RetryAfter as error:
            if attempt + 1 == TELEGRAM_ATTEMPTS:
                raise
            retry_after = error.retry_after
            delay = retry_after.total_seconds() if isinstance(retry_after, timedelta) else retry_after
            await asyncio.sleep(max(1, delay))
        except (NetworkError, TimedOut):
            if attempt + 1 == TELEGRAM_ATTEMPTS:
                raise
            await asyncio.sleep(2**attempt)
    raise TelegramError("Telegram не ответил после нескольких попыток.")


def offer_keyboard(app_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton(
                "Открыть в Steam",
                url=f"https://store.steampowered.com/app/{app_id}/",
            )
        ]]
    )


async def publish_deals(application: Application, config: Config) -> int:
    deals, active_app_ids, free_games = await fetch_deals(config)
    if active_app_ids is not None:
        mark_missing_offers_inactive(config.database_path, active_app_ids)
    published_count = 0
    failed_names = []
    moscow_date = datetime.now(MOSCOW_TIME).date().isoformat()
    posts = [(deal, False) for deal in deals]
    posts.extend((game, True) for game in free_games)
    for deal, is_free_game in posts:
        if published_count >= config.max_posts_per_check:
            break
        app_id = str(deal["id"])
        if is_free_game:
            if not can_publish_free_game(config.database_path, app_id, moscow_date):
                continue
            final_price = 0
            offer_expiration = None
        else:
            final_price = int(deal["final_price"])
            expiration = deal.get("discount_expiration")
            offer_expiration = (
                int(expiration) if isinstance(expiration, (int, float)) else None
            )
            if not should_publish(
                config.database_path, app_id, final_price, offer_expiration
            ):
                continue
        text = render_deal(deal)
        image = deal_image(deal)
        try:
            if image:
                try:
                    await send_with_retry(
                        lambda: application.bot.send_photo(
                            chat_id=config.channel_id,
                            photo=image,
                            caption=text,
                            parse_mode="HTML",
                            reply_markup=offer_keyboard(app_id),
                        )
                    )
                except TelegramError:
                    logging.exception(
                        "Не удалось отправить обложку игры %s; отправляем текстом", app_id
                    )
                    await send_with_retry(
                        lambda: application.bot.send_message(
                            chat_id=config.channel_id,
                            text=text,
                            parse_mode="HTML",
                            disable_web_page_preview=False,
                            reply_markup=offer_keyboard(app_id),
                        )
                    )
            else:
                await send_with_retry(
                    lambda: application.bot.send_message(
                        chat_id=config.channel_id,
                        text=text,
                        parse_mode="HTML",
                        disable_web_page_preview=False,
                        reply_markup=offer_keyboard(app_id),
                    )
                )
        except TelegramError:
            logging.exception("Не удалось опубликовать предложение Steam для игры %s", app_id)
            failed_names.append(str(deal.get("name") or app_id))
            continue
        if is_free_game:
            mark_free_game_published(config.database_path, app_id, moscow_date)
        else:
            mark_published(config.database_path, app_id, final_price, offer_expiration)
        published_count += 1
    if failed_names:
        raise PublicationError(
            "Не удалось опубликовать некоторые предложения: " + ", ".join(failed_names)
        )
    return published_count


async def notify_admins(
    application: Application, config: Config, message: str
) -> None:
    for user_id in config.admin_user_ids:
        try:
            await application.bot.send_message(chat_id=user_id, text=message)
        except TelegramError:
            logging.exception("Не удалось отправить уведомление администратору %s", user_id)


async def scheduled_check(context: ContextTypes.DEFAULT_TYPE) -> None:
    config: Config = context.application.bot_data["config"]
    try:
        count = await publish_deals(context.application, config)
        logging.info("Проверка Steam завершена; опубликовано предложений: %s.", count)
    except (httpx.HTTPError, ValueError, TelegramError):
        logging.exception("Не удалось проверить предложения Steam.")
        await notify_admins(
            context.application,
            config,
            "Не удалось проверить предложения Steam. Подробности есть в журнале GitHub Actions.",
        )


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message:
        await update.message.reply_text(
            "Я публикую в настроенный канал скидки на популярные игры Steam "
            "и временные раздачи платных игр."
        )


async def check_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or update.effective_user.id not in context.application.bot_data[
        "config"
    ].admin_user_ids:
        if update.message:
            await update.message.reply_text("Команда доступна только администраторам бота.")
        return
    if not update.message:
        return

    await update.message.reply_text("Проверяю предложения Steam…")
    try:
        count = await publish_deals(
            context.application, context.application.bot_data["config"]
        )
    except (httpx.HTTPError, ValueError, TelegramError):
        logging.exception("Не удалось вручную проверить предложения Steam.")
        await update.message.reply_text("Не удалось проверить Steam. Подробности в журнале бота.")
        await notify_admins(
            context.application,
            context.application.bot_data["config"],
            "Не удалось проверить предложения Steam. Подробности есть в журнале бота.",
        )
        return
    await update.message.reply_text(f"Опубликовано новых предложений: {count}.")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--once",
        action="store_true",
        help="Проверить Steam, опубликовать предложения и завершить работу.",
    )
    args = parser.parse_args()
    load_dotenv()
    logging.basicConfig(
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    )
    config = load_config()
    initialize_database(config.database_path)
    application = Application.builder().token(config.token).build()
    application.bot_data["config"] = config
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("check", check_command))
    if args.once:
        async def run_once() -> None:
            await application.initialize()
            try:
                count = await publish_deals(application, config)
                logging.info("Проверка Steam завершена; опубликовано предложений: %s.", count)
            except (httpx.HTTPError, ValueError, TelegramError):
                logging.exception("Не удалось проверить предложения Steam.")
                await notify_admins(
                    application,
                    config,
                    "Не удалось проверить предложения Steam. Подробности есть в журнале GitHub Actions.",
                )
                raise
            finally:
                await application.shutdown()

        asyncio.run(run_once())
        return

    application.job_queue.run_repeating(
        scheduled_check,
        interval=timedelta(minutes=config.check_interval_minutes),
        first=10,
    )
    application.run_polling()


if __name__ == "__main__":
    main()
