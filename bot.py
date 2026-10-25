import argparse
import asyncio
import logging
import os
import sqlite3
from dataclasses import dataclass
from datetime import timedelta

import httpx
from dotenv import load_dotenv
from telegram import Update
from telegram.error import TelegramError
from telegram.ext import Application, CommandHandler, ContextTypes

from steam_deals import deal_image, eligible_deals, render_deal

STEAM_FEATURED_URL = "https://store.steampowered.com/api/featuredcategories/"


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

    interval = int(os.environ.get("CHECK_INTERVAL_MINUTES", "30"))
    minimum_discount = int(os.environ.get("MINIMUM_DISCOUNT_PERCENT", "20"))
    max_posts = int(os.environ.get("MAX_POSTS_PER_CHECK", "10"))
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
            "lowest_final_price INTEGER)"
        )
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(published_deals)")
        }
        if "lowest_final_price" not in columns:
            connection.execute(
                "ALTER TABLE published_deals ADD COLUMN lowest_final_price INTEGER"
            )


def should_publish(path: str, app_id: str, final_price: int) -> bool:
    with sqlite3.connect(path) as connection:
        result = connection.execute(
            "SELECT lowest_final_price FROM published_deals WHERE app_id = ?", (app_id,)
        ).fetchone()
        if result is None:
            return True
        lowest_final_price = result[0]
        if lowest_final_price is None:
            connection.execute(
                "UPDATE published_deals SET lowest_final_price = ? WHERE app_id = ?",
                (final_price, app_id),
            )
            return False
        return final_price < lowest_final_price


def mark_published(path: str, app_id: str, final_price: int) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute(
            "INSERT INTO published_deals (app_id, lowest_final_price) VALUES (?, ?) "
            "ON CONFLICT(app_id) DO UPDATE SET lowest_final_price = "
            "CASE WHEN published_deals.lowest_final_price IS NULL "
            "THEN excluded.lowest_final_price ELSE "
            "MIN(published_deals.lowest_final_price, excluded.lowest_final_price) END",
            (app_id, final_price),
        )


async def fetch_deals(config: Config) -> list[dict]:
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.get(
            STEAM_FEATURED_URL,
            params={"cc": config.country_code, "l": config.language},
        )
        response.raise_for_status()
        payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError("Steam вернул данные в неожиданном формате.")
    return eligible_deals(payload, config.minimum_discount)


async def publish_deals(application: Application, config: Config) -> int:
    deals = await fetch_deals(config)
    published_count = 0
    failed_names = []
    for deal in deals:
        app_id = str(deal["id"])
        final_price = int(deal["final_price"])
        if not should_publish(config.database_path, app_id, final_price):
            continue
        text = render_deal(deal)
        image = deal_image(deal)
        try:
            if image:
                try:
                    await application.bot.send_photo(
                        chat_id=config.channel_id,
                        photo=image,
                        caption=text,
                        parse_mode="HTML",
                    )
                except TelegramError:
                    logging.exception(
                        "Не удалось отправить обложку игры %s; отправляем текстом", app_id
                    )
                    await application.bot.send_message(
                        chat_id=config.channel_id,
                        text=text,
                        parse_mode="HTML",
                        disable_web_page_preview=False,
                    )
            else:
                await application.bot.send_message(
                    chat_id=config.channel_id,
                    text=text,
                    parse_mode="HTML",
                    disable_web_page_preview=False,
                )
        except TelegramError:
            logging.exception("Не удалось опубликовать предложение Steam для игры %s", app_id)
            failed_names.append(str(deal.get("name") or app_id))
            continue
        mark_published(config.database_path, app_id, final_price)
        published_count += 1
        if published_count >= config.max_posts_per_check:
            break
    if failed_names:
        await notify_admins(
            application,
            config,
            "Не удалось опубликовать предложения в канал:\n"
            + "\n".join(f"• {name}" for name in failed_names),
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
    except (httpx.HTTPError, ValueError):
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
    except (httpx.HTTPError, ValueError):
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
            except (httpx.HTTPError, ValueError):
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
