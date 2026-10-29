"""H6：分钟线只保留连续竞价时段 09:30—11:30、13:00—15:00；15:00 后的行不混入 1m。
主板 15:01 之后的行是异常尾巴，直接剔除并计数；创业板／科创板盘后定价（15:05—15:30）单独放
after_hours_pricing，不混进 bars。"""
from __future__ import annotations

import unittest

from ym_stock_data.contracts import TZ_SHANGHAI
from ym_stock_data.providers.local import annotate_minute_bars
from datetime import datetime

DAY = "2026-10-29"


def bar(stamp, price=16.0):
    return {"time": f"{DAY} {stamp}:00", "open": price, "high": price, "low": price, "close": price,
            "vol": 1000.0, "amount": price * 1000.0}


def labels(result):
    return [row["time"][11:16] for row in result["bars"]]


def run(code, stamps):
    raw = {"code": code, "bars": [bar(s, 16.0 + i * 0.01) for i, s in enumerate(stamps)]}
    now = datetime.fromisoformat(f"{DAY}T16:30:00").replace(tzinfo=TZ_SHANGHAI)
    return annotate_minute_bars(raw, {"code": code, "period": "1m"}, source="tencent", now=now)


SESSION = ["09:30", "09:31", "11:29", "11:30", "13:00", "13:01", "14:59", "15:00"]
TAIL = ["15:06", "15:10", "15:20", "15:26"]
LUNCH = ["11:31", "12:00", "12:59"]


class SessionFilter(unittest.TestCase):
    def test_main_board_drops_everything_after_1500(self):
        result = run("603716", SESSION + TAIL)
        self.assertEqual(SESSION, labels(result))
        self.assertEqual(len(TAIL), result["post_session_bars_dropped"])
        self.assertEqual(SESSION[-1], labels(result)[-1])

    def test_lunch_gap_rows_are_not_session_bars(self):
        result = run("603716", ["09:30", "11:30"] + LUNCH + ["13:00", "15:00"])
        self.assertEqual(["09:30", "11:30", "13:00", "15:00"], labels(result))
        self.assertEqual(len(LUNCH), result["off_session_bars_dropped"])

    def test_chinext_after_hours_pricing_is_kept_apart(self):
        result = run("300045", SESSION + TAIL)
        self.assertEqual(SESSION, labels(result))
        after = result["after_hours_pricing"]
        self.assertEqual(TAIL, [row["time"][11:16] for row in after["bars"]])
        self.assertIn("盘后", after["note"])
        self.assertEqual(0, result["post_session_bars_dropped"])

    def test_star_market_is_also_after_hours_pricing(self):
        result = run("688018", SESSION + ["15:06"])
        self.assertEqual(["15:06"], [row["time"][11:16] for row in result["after_hours_pricing"]["bars"]])

    def test_counts_and_ma_ignore_the_tail(self):
        result = run("603716", SESSION + TAIL)
        self.assertEqual(len(SESSION), result["total_bars"])
        self.assertEqual(len(SESSION), result["returned_bars"])
        closes = [row["close"] for row in result["bars"]]
        self.assertEqual(closes[-1], result["last_close"])

    def test_no_tail_means_no_extra_fields_noise(self):
        result = run("603716", SESSION)
        self.assertEqual(0, result["post_session_bars_dropped"])
        self.assertNotIn("after_hours_pricing", result)

    def test_semantics_document_the_session_window(self):
        window = run("603716", SESSION)["bar_semantics"]["session_window"]
        self.assertEqual(["09:30", "15:00"], [window["start"], window["end"]])
        self.assertEqual(["11:30", "13:00"], window["lunch_gap"])


if __name__ == "__main__":
    unittest.main()
