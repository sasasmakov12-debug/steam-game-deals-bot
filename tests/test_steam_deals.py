import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
from telegram.error import TimedOut
from bot import (
    can_publish_free_game,
    initialize_database,
    mark_free_game_published,
    mark_missing_offers_inactive,
    mark_published,
    offer_keyboard,
    request_with_retry,
    send_with_retry,
    should_publish,
)
from steam_deals import (
    active_special_ids,
    deal_image,
    eligible_deals,
    parse_free_games,
    render_deal,
)


class EligibleDealsTests(unittest.TestCase):
    def setUp(self):
        self.payload = {
            "top_sellers": {"items": [{"id": 10}, {"id": 11}, {"id": 12}]},
            "new_releases": {
                "items": [
                    {
                        "id": 14,
                        "type": 0,
                        "name": "Новая игра",
                        "discounted": True,
                        "discount_percent": 40,
                        "original_price": 2000,
                        "final_price": 1200,
                        "currency": "RUB",
                    }
                ]
            },
            "specials": {
                "items": [
                    {
                        "id": 10,
                        "name": "Со скидкой",
                        "discounted": True,
                        "discount_percent": 25,
                        "original_price": 1000,
                        "final_price": 750,
                        "currency": "RUB",
                        "large_capsule_image": "https://cdn.example.test/game.jpg",
                        "discount_expiration": 1793901600,
                    },
                    {
                        "id": 11,
                        "name": "Временно бесплатно",
                        "discounted": True,
                        "discount_percent": 100,
                        "original_price": 1000,
                        "final_price": 0,
                        "currency": "RUB",
                        "discount_expiration": 1793901600,
                    },
                    {
                        "id": 12,
                        "name": "Бесплатная игра",
                        "discounted": False,
                        "discount_percent": 0,
                        "original_price": 0,
                        "final_price": 0,
                        "currency": "RUB",
                    },
                    {
                        "id": 13,
                        "name": "Не в списке популярных",
                        "discounted": True,
                        "discount_percent": 50,
                        "original_price": 1000,
                        "final_price": 500,
                        "currency": "RUB",
                    },
                ]
            },
        }

    def test_returns_discounted_top_sellers_and_temporary_paid_giveaways(self):
        deals = eligible_deals(self.payload)
        self.assertEqual([deal["id"] for deal in deals], [10, 11, 14])
        self.assertEqual(deals[-1]["popularity_rank"], 1000)

    def test_sorts_discounted_games_by_steam_popularity(self):
        deals = eligible_deals(self.payload)
        self.assertLess(
            deals.index(next(deal for deal in deals if deal["id"] == 10)),
            deals.index(next(deal for deal in deals if deal["id"] == 11)),
        )
        self.assertLess(
            deals.index(next(deal for deal in deals if deal["id"] == 10)),
            deals.index(next(deal for deal in deals if deal["id"] == 14)),
        )

    def test_returns_active_discounted_app_ids(self):
        self.assertEqual(active_special_ids(self.payload), {"10", "11", "13", "14"})
        self.assertEqual(active_special_ids({"specials": {"items": []}}), set())
        self.assertIsNone(active_special_ids({}))

    def test_honors_minimum_discount(self):
        deals = eligible_deals(self.payload, minimum_discount=30)
        self.assertEqual([deal["id"] for deal in deals], [11, 14])

    def test_formats_free_giveaway(self):
        deal = next(deal for deal in eligible_deals(self.payload) if deal["id"] == 11)
        text = render_deal(deal)
        self.assertIn("Можно забрать навсегда", text)
        self.assertIn("Бесплатно", text)
        self.assertIn("https://store.steampowered.com/app/11/", text)
        self.assertIn("Акция до: ", text)

    def test_returns_steam_capsule_image(self):
        deal = next(deal for deal in eligible_deals(self.payload) if deal["id"] == 10)
        self.assertEqual(
            deal_image(deal), "https://cdn.example.test/game.jpg"
        )

    def test_parses_only_free_steam_search_results(self):
        results_html = """
        <a class="search_result_row" data-ds-appid="123" data-ds-tagids="[113,1718]">
          <div class="search_capsule"><img src="https://cdn.example.test/free.jpg"></div>
          <span class="title">Бесплатная игра</span>
          <div>Бесплатно</div>
        </a>
        <a class="search_result_row" data-ds-appid="456" data-ds-tagids="[19]">
          <span class="title">Игра со скидкой</span><div>Скидка — 50%</div>
        </a>
        """
        games = parse_free_games(results_html)
        self.assertEqual(len(games), 1)
        self.assertEqual(games[0]["id"], 123)
        self.assertEqual(games[0]["name"], "Бесплатная игра")
        self.assertTrue(games[0]["is_free_to_play"])
        self.assertNotIn("_is_free_to_play", games[0])
        self.assertEqual(
            deal_image(games[0]), "https://cdn.example.test/free.jpg"
        )

    def test_builds_steam_button(self):
        button = offer_keyboard("123").inline_keyboard[0][0]
        self.assertEqual(button.text, "Открыть в Steam")
        self.assertEqual(button.url, "https://store.steampowered.com/app/123/")

    def test_only_publishes_new_or_lower_price_offers(self):
        with tempfile.TemporaryDirectory() as directory:
            database = str(Path(directory) / "deals.sqlite3")
            initialize_database(database)
            self.assertTrue(should_publish(database, "10", 750, 1000))
            mark_published(database, "10", 750, 1000)
            self.assertFalse(should_publish(database, "10", 750, 1000))
            self.assertFalse(should_publish(database, "10", 900, 1000))
            self.assertTrue(should_publish(database, "10", 500, 1000))
            mark_published(database, "10", 500, 1000)
            self.assertFalse(should_publish(database, "10", 700, 1000))

    def test_republishes_same_price_after_offer_disappears(self):
        with tempfile.TemporaryDirectory() as directory:
            database = str(Path(directory) / "deals.sqlite3")
            initialize_database(database)
            mark_published(database, "10", 750, 1000)
            mark_missing_offers_inactive(database, set())
            self.assertTrue(should_publish(database, "10", 750, 1000))

    def test_republishes_when_offer_expiration_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            database = str(Path(directory) / "deals.sqlite3")
            initialize_database(database)
            mark_published(database, "10", 750, 1000)
            self.assertTrue(should_publish(database, "10", 750, 2000))

    def test_migrates_existing_publication_history_without_reposting(self):
        with tempfile.TemporaryDirectory() as directory:
            database = str(Path(directory) / "deals.sqlite3")
            with sqlite3.connect(database) as connection:
                connection.execute(
                    "CREATE TABLE published_deals "
                    "(app_id TEXT PRIMARY KEY, published_at TEXT NOT NULL "
                    "DEFAULT CURRENT_TIMESTAMP)"
                )
                connection.execute(
                    "INSERT INTO published_deals (app_id) VALUES ('10')"
                )
            initialize_database(database)
            self.assertFalse(should_publish(database, "10", 600, 1000))
            self.assertTrue(should_publish(database, "10", 500, 1000))

    def test_limits_new_free_game_posts_to_one_per_day(self):
        with tempfile.TemporaryDirectory() as directory:
            database = str(Path(directory) / "deals.sqlite3")
            initialize_database(database)
            self.assertTrue(can_publish_free_game(database, "10", "2026-10-28"))
            mark_free_game_published(database, "10", "2026-10-28")
            self.assertFalse(can_publish_free_game(database, "10", "2026-10-28"))
            self.assertFalse(can_publish_free_game(database, "11", "2026-10-28"))
            self.assertTrue(can_publish_free_game(database, "11", "2026-10-29"))

    def test_does_not_repeat_free_game_on_future_days(self):
        with tempfile.TemporaryDirectory() as directory:
            database = str(Path(directory) / "deals.sqlite3")
            initialize_database(database)
            mark_free_game_published(database, "10", "2026-10-28")
            self.assertFalse(can_publish_free_game(database, "10", "2026-10-29"))


class RetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_retries_steam_server_errors(self):
        attempts = 0

        async def handler(request):
            nonlocal attempts
            attempts += 1
            status = 503 if attempts < 3 else 200
            return httpx.Response(status, json={"ok": True})

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as client:
            with patch("bot.asyncio.sleep", new_callable=AsyncMock):
                response = await request_with_retry(client, "https://steam.test", {})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(attempts, 3)

    async def test_retries_temporary_telegram_network_error(self):
        send = AsyncMock(side_effect=[TimedOut("timeout"), "sent"])
        with patch("bot.asyncio.sleep", new_callable=AsyncMock):
            result = await send_with_retry(send)
        self.assertEqual(result, "sent")
        self.assertEqual(send.await_count, 2)


if __name__ == "__main__":
    unittest.main()
