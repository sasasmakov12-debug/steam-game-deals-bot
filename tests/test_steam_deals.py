import unittest

from steam_deals import eligible_deals, render_deal


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
                    },
                    {
                        "id": 11,
                        "name": "Временно бесплатно",
                        "discount_percent": 100,
                        "original_price": 1000,
                        "final_price": 0,
                        "currency": "RUB",
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


if __name__ == "__main__":
    unittest.main()
