import unittest
from contextlib import redirect_stdout
from io import StringIO
from unittest.mock import patch

from ym_stock_data import list_registered_intents as public_list_registered_intents
from ym_stock_data import resolve_intent as public_resolve_intent
from ym_stock_data.__main__ import main
from ym_stock_data.intent_registry import list_registered_intents, resolve_intent


class IntentRegistryTests(unittest.TestCase):
    def test_promotion_phrase_maps_to_dated_market_facts(self):
        self.assertEqual(
            {"intent": "market_facts", "params": {"trade_date": "20260923"}},
            resolve_intent("查晋级率", trade_date="20260923"),
        )
        self.assertEqual(
            {"intent": "market_facts", "params": {}}, resolve_intent("晋级率")
        )

    def test_limit_and_hot_phrases_map_to_fixed_stocktoday_apis(self):
        up = resolve_intent("查涨停板", trade_date="20260923")
        self.assertEqual("market_limit_board", up["intent"])
        self.assertEqual({"kind": "up", "date": "20260923"}, up["params"])

        down = resolve_intent("查跌停板")
        self.assertEqual("market_limit_board", down["intent"])
        self.assertEqual({"kind": "down"}, down["params"])

        ths = resolve_intent("查同花顺热榜")
        self.assertEqual("market_hot_rank", ths["intent"])
        self.assertEqual({"source": "ths"}, ths["params"])

        dc = resolve_intent("查东财热榜")
        self.assertEqual("market_hot_rank", dc["intent"])
        self.assertEqual({"source": "dc"}, dc["params"])

    def test_market_snapshot_and_kline_phrases_map_to_canonical_query(self):
        self.assertEqual(
            {"intent": "realtime_market", "params": {}},
            resolve_intent("查实时大盘"),
        )
        self.assertEqual(
            {"intent": "stock_snapshot", "params": {"codes": ["600519"]}},
            resolve_intent("查实时个股", codes=["600519"]),
        )
        self.assertEqual(
            {
                "intent": "stock_kline",
                "params": {"code": "600519", "period": "daily", "count": 3},
            },
            resolve_intent("查日K", code="600519", count=3),
        )
        self.assertEqual(
            {
                "intent": "stock_kline",
                "params": {"code": "600519", "period": "15m", "count": 2},
            },
            resolve_intent("查分钟K", code="600519", period="15m", count=2),
        )
        self.assertEqual(
            {
                "intent": "stock_kline",
                "params": {
                    "code": "600519",
                    "period": "daily",
                    "adjustment": "qfq",
                    "start_date": "20260101",
                    "end_date": "20260922",
                },
            },
            resolve_intent(
                "查前复权日K",
                code="600519",
                start_date="20260101",
                end_date="20260922",
            ),
        )

    def test_sector_and_industry_phrases_use_controlled_dataset_methods(self):
        sector = resolve_intent("查板块", names=["半导体"])
        self.assertEqual("sector_index", sector["intent"])
        self.assertEqual({"names": ["半导体"]}, sector["params"])

        industry = resolve_intent("查行业", names=["电子"])
        self.assertEqual("sector_index", industry["intent"])
        self.assertEqual({"names": ["电子"]}, industry["params"])

    def test_registry_is_exact_and_rejects_free_form_or_missing_parameters(self):
        self.assertIn("查涨停板", list_registered_intents())
        with self.assertRaises(ValueError):
            resolve_intent("查涨停板 今日")
        with self.assertRaises(ValueError):
            resolve_intent("查同花顺热榜", arbitrary="guess")
        with self.assertRaises(ValueError):
            resolve_intent("查实时个股")
        with self.assertRaises(ValueError):
            resolve_intent("查分钟K", code="600519")

    def test_registry_is_public_and_cli_uses_the_same_descriptor(self):
        self.assertIs(public_resolve_intent, resolve_intent)
        self.assertIs(public_list_registered_intents, list_registered_intents)
        output = StringIO()
        canonical_result = {"data": {}, "_meta": {"status": "empty"}}
        with patch(
            "ym_stock_data.__main__.canonical_query", return_value=canonical_result
        ) as canonical, redirect_stdout(output):
            exit_code = main(["intent", "查涨停板", 'trade_date="20260923"'])

        self.assertEqual(0, exit_code)
        canonical.assert_called_once_with(
            "market_limit_board",
            kind="up",
            date="20260923",
        )

    def test_cli_treats_unquoted_stock_codes_and_dates_as_strings(self):
        output = StringIO()
        canonical_result = {"data": {}, "_meta": {"status": "empty"}}
        with patch(
            "ym_stock_data.__main__.canonical_query", return_value=canonical_result
        ) as canonical, redirect_stdout(output):
            exit_code = main(
                ["intent", "查日K", "code=600519", "count=3"]
            )

        self.assertEqual(0, exit_code)
        canonical.assert_called_once_with(
            "stock_kline",
            code="600519",
            period="daily",
            count=3,
        )

        with patch(
            "ym_stock_data.__main__.canonical_query", return_value=canonical_result
        ) as canonical, redirect_stdout(StringIO()):
            main(["intent", "查同花顺热榜", "trade_date=20260922"])
        canonical.assert_called_once_with(
            "market_hot_rank",
            source="ths",
            trade_date="20260922",
        )


if __name__ == "__main__":
    unittest.main()
