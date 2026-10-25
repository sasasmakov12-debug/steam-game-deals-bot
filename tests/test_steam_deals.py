import sqlite3
import tempfile
import unittest
from pathlib import Path

from bot import initialize_database, mark_published, should_publish
from steam_deals import deal_image, eligible_deals, render_deal


class EligibleDealsTests(unittest.TestCase):
    def setUp(self):
        self.payload = {
            "top_sellers": {"items": [{"id": 10}, {"id": 11}, {"id": 12}]},
            "specials": {
                "items": [
                    {
                        "id": 10,
                        "name": "Со скидкой",
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
                        "discount_percent": 100,
                        "original_price": 1000,
                        "final_price": 0,
                        "currency": "RUB",
                        "discount_expiration": 1793901600,
                    },
                    {
                        "id": 12,
                        "name": "Бесплатная игра",
                        "discount_percent": 0,
                        "original_price": 0,
                        "final_price": 0,
                        "currency": "RUB",
                    },
                    {
                        "id": 13,
                        "name": "Не в списке популярных",
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
        self.assertEqual([deal["id"] for deal in deals], [10, 11])

    def test_honors_minimum_discount(self):
        deals = eligible_deals(self.payload, minimum_discount=30)
        self.assertEqual([deal["id"] for deal in deals], [11])

    def test_formats_free_giveaway(self):
        deal = eligible_deals(self.payload)[1]
        text = render_deal(deal)
        self.assertIn("Можно забрать навсегда", text)
        self.assertIn("Бесплатно", text)
        self.assertIn("https://store.steampowered.com/app/11/", text)
        self.assertIn("Акция до: ", text)

    def test_returns_steam_capsule_image(self):
        deal = eligible_deals(self.payload)[0]
        self.assertEqual(
            deal_image(deal), "https://cdn.example.test/game.jpg"
        )

    def test_only_publishes_new_or_lower_price_offers(self):
        with tempfile.TemporaryDirectory() as directory:
            database = str(Path(directory) / "deals.sqlite3")
            initialize_database(database)
            self.assertTrue(should_publish(database, "10", 750))
            mark_published(database, "10", 750)
            self.assertFalse(should_publish(database, "10", 750))
            self.assertFalse(should_publish(database, "10", 900))
            self.assertTrue(should_publish(database, "10", 500))
            mark_published(database, "10", 500)
            self.assertFalse(should_publish(database, "10", 700))

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
            self.assertFalse(should_publish(database, "10", 600))
            self.assertTrue(should_publish(database, "10", 500))


if __name__ == "__main__":
    unittest.main()
