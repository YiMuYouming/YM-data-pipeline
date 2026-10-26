"""A3（2026-10-26 批次4）：腾讯行情成交量单位按板块区分。

实测（10-26 收盘后多票交叉验证，见 evidence/BATCH4/a3-*.json）：

- 科创板 688/689：腾讯快照字段 v6、分钟累计量、日 K vol 都是**股**；
- 沪深主板/创业板：同一字段是**手**（×100 = 股）；
- 北交所经腾讯这两个接口无成交量（实测 v6=0、日 K 空），不涉及换算。

修正前适配器行为正好两个方向都错：
- 快照 `fetch_quotes` 一律 ×100 → 科创板放大 100 倍（A3 原疑点 326,537,300）；
- 分钟 K 在 10-24/T4 之后一律不乘 → 主板/创业板少 100 倍。
"""

import json
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

from ym_stock_data import api, query
from ym_stock_data.a_share_codes import is_star_market_code
from ym_stock_data.provider_policy import CompiledPolicy
from ym_stock_data.provider_state import ProviderState
from ym_stock_data.providers.local import LocalProvider
from ym_stock_data.sources import tencent


_FIELDS = 88


def _qt_line(prefixed: str, *, name: str, price: float, volume: float, amount_wan: float,
             quote_time: str = "20261026150000") -> str:
    vals = ["0"] * _FIELDS
    vals[1] = name
    vals[3] = str(price)
    vals[4] = str(price)
    vals[5] = str(price)
    vals[6] = str(volume)
    vals[30] = quote_time
    vals[32] = "0"
    vals[37] = str(amount_wan)
    return f'v_{prefixed}="' + "~".join(vals) + '";'


def _response(body: str) -> Mock:
    response = Mock()
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    response.read.return_value = body.encode("gbk")
    return response


class StarMarketCodeTests(unittest.TestCase):
    def test_star_market_boundaries(self):
        for code in ("688018", "688001", "689009", "688981.SH"):
            with self.subTest(code=code):
                self.assertTrue(is_star_market_code(code))
        for code in ("600519", "601318", "300458", "301536", "000001", "920002", "832566"):
            with self.subTest(code=code):
                self.assertFalse(is_star_market_code(code))


class TencentSnapshotVolumeUnitTests(unittest.TestCase):
    def test_snapshot_volume_uses_board_unit(self):
        # 688018 收盘 15,502,563 股（v6 原值即股）；600519 41,599 手；300458 340,460 手。
        body = (
            _qt_line("sh688018", name="乐鑫科技", price=143.33, volume=15502563, amount_wan=229508)
            + _qt_line("sh600519", name="贵州茅台", price=1349.98, volume=41599, amount_wan=557429)
            + _qt_line("sz300458", name="全志科技", price=28.60, volume=340460, amount_wan=99090)
        )
        with patch("ym_stock_data.sources.tencent.urllib.request.urlopen", return_value=_response(body)):
            quotes = tencent.fetch_quotes(["688018", "600519", "300458"])

        # 科创板：原值就是股，不得再乘 100
        self.assertEqual(15_502_563.0, quotes["688018"]["volume"])
        # 主板/创业板：原值是手，乘 100 换算成股
        self.assertEqual(4_159_900.0, quotes["600519"]["volume"])
        self.assertEqual(34_046_000.0, quotes["300458"]["volume"])
        # 额保持 CNY（万元 × 10000）
        self.assertEqual(2_295_080_000.0, quotes["688018"]["amount"])

    def test_public_snapshot_volume_matches_amount_and_price_for_each_board(self):
        body = (
            _qt_line("sh688018", name="乐鑫科技", price=143.33, volume=15502563, amount_wan=229508)
            + _qt_line("sh600519", name="贵州茅台", price=1349.98, volume=41599, amount_wan=557429)
            + _qt_line("sz300458", name="全志科技", price=28.60, volume=340460, amount_wan=99090)
        )
        with patch("ym_stock_data.sources.tencent.urllib.request.urlopen", return_value=_response(body)):
            quotes = tencent.fetch_quotes(["688018", "600519", "300458"])
        for code in ("688018", "600519", "300458"):
            with self.subTest(code=code):
                row = quotes[code]
                implied = row["amount"] / row["price"]
                # 量额价自洽：容差 15%（当日均价与收盘价不同、四舍五入）
                self.assertLess(abs(row["volume"] - implied) / implied, 0.15)


class TencentMinuteVolumeUnitTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        state = ProviderState(Path(temp.name) / "state.sqlite3")
        for target, value in [
            ("_STATE", state),
            ("load_compiled_policy", lambda: CompiledPolicy(None, "inactive", None)),
            ("_now_shanghai", lambda: datetime(2026, 10, 26, 10, 5, tzinfo=timezone(timedelta(hours=8)))),
            ("_provider_for", lambda name: LocalProvider(name)),
        ]:
            patched = patch.object(api, target, value)
            patched.start()
            self.addCleanup(patched.stop)

    def _query_minute(self, code: str, payload: dict, **params):
        response = _response(json.dumps(payload))
        with patch("ym_stock_data.sources.pytdx.urllib.request.urlopen", return_value=response):
            return query("stock_kline", code=code, period=params.pop("period", "5m"), **params)

    def test_minute_cumulative_volume_uses_board_unit(self):
        star_payload = {"code": 0, "data": {"sh688018": {"data": [
            {"date": "20261026", "data": ["0930 143.33 15502563 2295076716.00"]}
        ]}}}
        main_payload = {"code": 0, "data": {"sh600519": {"data": [
            {"date": "20261026", "data": ["0930 1349.98 41599 5574290000.00"]}
        ]}}}
        growth_payload = {"code": 0, "data": {"sz300458": {"data": [
            {"date": "20261026", "data": ["0930 28.60 340460 990900000.00"]}
        ]}}}

        star = self._query_minute("688018", star_payload, start_date="20261026", end_date="20261026", count=20)
        main = self._query_minute("600519", main_payload, start_date="20261026", end_date="20261026", count=20)
        growth = self._query_minute("300458", growth_payload, start_date="20261026", end_date="20261026", count=20)

        self.assertEqual("tencent", star["_meta"]["provider_used"])
        self.assertEqual(15_502_563.0, sum(b["volume"] for b in star["data"]["bars"]))
        self.assertEqual(4_159_900.0, sum(b["volume"] for b in main["data"]["bars"]))
        self.assertEqual(34_046_000.0, sum(b["volume"] for b in growth["data"]["bars"]))
        for result in (star, main, growth):
            self.assertEqual("share", result["data"]["volume_unit"])
            self.assertEqual("CNY", result["data"]["amount_unit"])

    def test_tencent_daily_kline_volume_uses_board_unit(self):
        star_daily = {"code": 0, "data": {"sh688018": {"day": [
            ["2026-10-26", "151.50", "143.33", "152.80", "142.40", "15502563"]
        ]}}}
        main_daily = {"code": 0, "data": {"sh600519": {"day": [
            ["2026-10-26", "1325.40", "1349.98", "1355.00", "1310.00", "41599"]
        ]}}}
        with patch("ym_stock_data.sources.pytdx.urllib.request.urlopen", return_value=_response(json.dumps(star_daily))):
            star = LocalProvider._http_kline(
                provider="tencent",
                params={"code": "688018", "period": "daily", "count": 1, "adjustment": "none"},
            )
        with patch("ym_stock_data.sources.pytdx.urllib.request.urlopen", return_value=_response(json.dumps(main_daily))):
            main = LocalProvider._http_kline(
                provider="tencent",
                params={"code": "600519", "period": "daily", "count": 1, "adjustment": "none"},
            )
        self.assertEqual(15_502_563.0, star["bars"][0]["volume"])
        self.assertEqual(4_159_900.0, main["bars"][0]["volume"])
        self.assertEqual("share", star["volume_unit"])
        self.assertEqual("share", main["volume_unit"])


if __name__ == "__main__":
    unittest.main()
