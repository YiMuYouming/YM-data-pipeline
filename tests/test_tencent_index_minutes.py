import unittest
from unittest.mock import patch

from ym_stock_data.routing import route_for
from ym_stock_data.sources import tencent


def day(date, first_price, amounts):
    """Cumulative minute rows 0930, 0931..1130, 1301..1500 with a given amount per minute."""
    clocks = ["0930"] + [f"{9 + (30 + m) // 60:02d}{(30 + m) % 60:02d}" for m in range(1, 121)] \
        + [f"{13 + m // 60:02d}{m % 60:02d}" for m in range(1, 121)]
    rows, volume, amount = [], 0, 0.0
    for index, clock in enumerate(clocks):
        volume += 10
        amount += amounts
        rows.append(f"{clock} {first_price + index * 0.01:.2f} {volume} {amount:.2f}")
    return {"date": date, "data": rows}


PAYLOAD = {"code": 0, "data": {"sh000001": {"data": [day("20260924", 3900.0, 2e8), day("20260923", 3950.0, 1e8)]}}}


class TencentIndexMinuteTests(unittest.TestCase):
    def test_minutes_aggregate_to_session_aligned_bars_with_amounts(self):
        with patch.object(tencent, "_get_json", return_value=PAYLOAD):
            result = tencent.fetch_index_minute_bars("000001.SH", period="15m")
        bars = [bar for bar in result["bars"] if bar["datetime"].startswith("2026-09-24")]
        self.assertEqual(16, len(bars))
        self.assertEqual(["2026-09-24 09:45", "2026-09-24 11:30", "2026-09-24 13:15", "2026-09-24 15:00"],
                         [bars[0]["datetime"], bars[7]["datetime"], bars[8]["datetime"], bars[-1]["datetime"]])
        # first bar holds 0930..0945 = 16 minutes; later bars 15 minutes each
        self.assertEqual(16 * 2e8, bars[0]["amount"])
        self.assertEqual(15 * 2e8, bars[1]["amount"])
        self.assertEqual(15 * 10 * 100, bars[1]["volume"])
        self.assertEqual("CNY", result["amount_unit"])
        with patch.object(tencent, "_get_json", return_value=PAYLOAD):
            hourly = tencent.fetch_index_minute_bars("000001.SH", period="60m")
        self.assertEqual(["10:30", "11:30", "14:00", "15:00"],
                         [bar["datetime"][11:] for bar in hourly["bars"] if bar["datetime"].startswith("2026-09-24")])

    def test_compare_uses_previous_day_same_slot(self):
        payloads = {code: {"code": 0, "data": {code: {"data": [day("20260924", 1.0, 2e8), day("20260923", 1.0, 1e8)]}}}
                    for code in ("sh000001", "sz399001", "sz399006")}
        with patch.object(tencent, "_get_json", side_effect=lambda url: payloads[url.split("code=")[1]]):
            result = tencent.fetch_index_intraday_compare(period="15m", trade_date="20260924")
        rows = result["上证15min"]
        self.assertEqual("09:45", rows[0]["t"])
        self.assertEqual(16 * 1e8, rows[0]["yesterdayAmt"])
        self.assertTrue(rows[-1]["_cum"])
        self.assertEqual("tencent", result["source"])

    def test_index_intraday_compare_route_is_tencent_primary(self):
        for params in ({"period": "15m"}, {"period": "15m", "use_case": "realtime_poll"}):
            self.assertEqual(("tencent",), route_for("index_intraday_compare", params).providers)


if __name__ == "__main__":
    unittest.main()
