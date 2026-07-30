from __future__ import annotations

import inspect
import unittest
from unittest.mock import patch

import ym_stock_data.api as api
from ym_stock_data.providers.base import ProviderOutcome
from ym_stock_data.provider_state import ProviderState
from ym_stock_data.pytdx_screener_query import compile_pytdx_screener_query
from ym_stock_data.providers.pytdx_screener import PytdxScreenerProvider
from ym_stock_data.routing import route_for


class CompilerTests(unittest.TestCase):
    def test_compiles_only_the_reviewed_structured_grammar(self):
        cases = (
            "沪深A股 非ST 非停牌 最新价>=10 涨幅<5%",
            "沪市A股且非停牌且最新价10到20且涨幅-2%至3%",
            "上交所A股；股票代码为600519；非ST",
            "深市A股并且股票代码=000001并且非停牌",
            "深交所A股非ST",
            "沪深Ａ股，非ＳＴ，非停牌，最新价＞＝10",
        )
        for query in cases:
            with self.subTest(query=query):
                compiled = compile_pytdx_screener_query(query)
                self.assertIsNotNone(compiled)

    def test_rejects_generic_multiple_or_filterless_universes(self):
        for query in (
            "A股 非ST",
            "沪深 非ST",
            "沪深A股",
            "沪市A股 深市A股 非ST",
            "北交所A股 非ST",
        ):
            with self.subTest(query=query):
                self.assertIsNone(compile_pytdx_screener_query(query))

    def test_rejects_unsupported_or_partially_consumed_language(self):
        for query in (
            "沪深A股 非ST 或 涨幅>3%",
            "沪深A股 非ST OR 非停牌",
            "沪深A股 半导体 非ST",
            "沪深A股 IGBT概念 非ST",
            "沪深A股 PE<20 非ST",
            "沪深A股 总市值>100亿 非ST",
            "沪深A股 涨幅排名前10 非停牌",
            "沪深A股 2026年7月30日 非ST",
            "沪深A股 非st rubbish",
            "沪深A股 股票代码为600519和000001",
            "沪深A股 股价>10 非停牌",
        ):
            with self.subTest(query=query):
                self.assertIsNone(compile_pytdx_screener_query(query))

    def test_numeric_filters_require_non_suspended_filter(self):
        for query in (
            "沪深A股 最新价>10",
            "沪深A股 涨幅>=3%",
            "沪市A股 最新价10~20 非ST",
        ):
            with self.subTest(query=query):
                self.assertIsNone(compile_pytdx_screener_query(query))

    def test_single_code_must_be_six_digits_and_match_universe(self):
        accepted = (
            "沪市A股 股票代码是688001",
            "上交所A股 股票代码=600519",
            "深市A股 股票代码为300001",
        )
        rejected = (
            "沪市A股 股票代码是000001",
            "深交所A股 股票代码=600519",
            "沪深A股 股票代码为920001",
            "沪深A股 股票代码为60051",
            "沪深A股 股票代码600519",
        )
        for query in accepted:
            with self.subTest(query=query):
                self.assertIsNotNone(compile_pytdx_screener_query(query))
        for query in rejected:
            with self.subTest(query=query):
                self.assertIsNone(compile_pytdx_screener_query(query))

    def test_comparison_and_range_boundaries_are_explicit(self):
        compiled = compile_pytdx_screener_query(
            "沪深A股 非停牌 最新价>=10 最新价<20 涨幅-2%到3%"
        )
        self.assertIsNotNone(compiled)
        self.assertTrue(compiled.matches(price=10, pct_change=-2))
        self.assertTrue(compiled.matches(price=19.99, pct_change=3))
        self.assertFalse(compiled.matches(price=20, pct_change=0))
        self.assertFalse(compiled.matches(price=15, pct_change=3.01))


class FakeApi:
    def __init__(self, directories, quotes, *, connects=True):
        self.directories = directories
        self.quotes = quotes
        self.connects = connects
        self.quote_batches = []
        self.list_calls = []
        self.disconnect_calls = 0

    def connect(self, _host, _port, *, time_out):
        self.time_out = time_out
        return self.connects

    def disconnect(self):
        self.disconnect_calls += 1

    def get_security_count(self, market):
        return len(self.directories[market])

    def get_security_list(self, market, start):
        self.list_calls.append((market, start))
        return self.directories[market][start : start + 1000]

    def get_security_quotes(self, batch):
        self.quote_batches.append(list(batch))
        return [self.quotes[key] for key in batch if key in self.quotes]


def quote(code, price, last_close):
    return {"code": code, "price": price, "last_close": last_close}


class ProviderTests(unittest.TestCase):
    def provider(self, fake):
        return PytdxScreenerProvider(
            api_factory=lambda: fake,
            servers=(("fake-host", 7709),),
            connect_timeout=1,
        )

    def test_probe_is_offline_and_auth_free(self):
        factory = unittest.mock.Mock(side_effect=AssertionError("must stay offline"))
        provider = PytdxScreenerProvider(api_factory=factory)

        report = provider.probe()

        self.assertEqual("configured_unverified", report["status"])
        self.assertEqual({"required": False, "status": "not_required"}, report["auth"])
        factory.assert_not_called()

    def test_reads_complete_directories_and_quotes_then_filters_and_sorts(self):
        directories = {
            0: [
                {"code": "000002", "name": "万科A"},
                {"code": "000001", "name": "平安银行"},
                {"code": "300001", "name": "ST特锐"},
                {"code": "399001", "name": "深证成指"},
            ],
            1: [
                {"code": "600001", "name": "邯郸钢铁"},
                {"code": "600000", "name": "浦发银行"},
                {"code": "688001", "name": "华兴源创"},
                {"code": "510050", "name": "50ETF"},
            ],
        }
        quotes = {
            (0, "000001"): quote("000001", 12, 10),
            (0, "000002"): quote("000002", 0, 10),
            (0, "300001"): quote("300001", 12, 10),
            (1, "600000"): quote("600000", 11, 10),
            (1, "600001"): quote("600001", 9, 10),
            (1, "688001"): quote("688001", 13, 10),
        }
        fake = FakeApi(directories, quotes)

        outcome = self.provider(fake).call(
            "review_sentiment",
            {
                "query": "沪深A股 非ST 非停牌 最新价>=10 涨幅>=10%",
                "limit": 2,
            },
        )

        self.assertEqual("success", outcome.status)
        self.assertEqual("pytdx_screener", outcome.provider)
        self.assertEqual(
            ["000001", "600000"],
            [row["股票代码"] for row in outcome.data["datas"]],
        )
        self.assertEqual(2, outcome.quality["returned_count"])
        self.assertEqual({"required": False, "status": "not_required"}, outcome.auth)
        self.assertEqual([(0, 0), (1, 0)], fake.list_calls)
        self.assertEqual(1, fake.disconnect_calls)

    def test_quotes_are_batched_at_eighty_or_less_before_sorted_limit(self):
        rows = [
            {"code": f"600{i:03d}", "name": f"股票{i:03d}"}
            for i in range(161)
        ]
        directories = {0: [], 1: list(reversed(rows))}
        quotes = {
            (1, row["code"]): quote(row["code"], 10, 10) for row in rows
        }
        fake = FakeApi(directories, quotes)

        outcome = self.provider(fake).call(
            "review_sentiment",
            {"query": "沪市A股 非ST", "limit": 3},
        )

        self.assertEqual("success", outcome.status)
        self.assertEqual([80, 80, 1], [len(batch) for batch in fake.quote_batches])
        self.assertEqual(
            ["600000", "600001", "600002"],
            [row["股票代码"] for row in outcome.data["datas"]],
        )

    def test_single_code_still_requires_complete_directory_but_only_quotes_code(self):
        directories = {
            0: [{"code": "000001", "name": "平安银行"}],
            1: [{"code": "600519", "name": "贵州茅台"}],
        }
        quotes = {(1, "600519"): quote("600519", 1500, 1400)}
        fake = FakeApi(directories, quotes)

        outcome = self.provider(fake).call(
            "review_sentiment",
            {"query": "沪深A股 股票代码为600519", "limit": 20},
        )

        self.assertEqual("success", outcome.status)
        self.assertEqual([[(1, "600519")]], fake.quote_batches)
        self.assertEqual([(0, 0), (1, 0)], fake.list_calls)

    def test_complete_data_may_produce_valid_empty(self):
        fake = FakeApi(
            {0: [], 1: [{"code": "600519", "name": "贵州茅台"}]},
            {(1, "600519"): quote("600519", 10, 10)},
        )

        outcome = self.provider(fake).call(
            "review_sentiment",
            {"query": "沪市A股 非停牌 最新价>100", "limit": 20},
        )

        self.assertEqual("empty", outcome.status)
        self.assertEqual({"datas": [], "row_count": 0}, outcome.data)

    def test_incomplete_directory_or_quotes_never_become_empty(self):
        class ShortDirectoryApi(FakeApi):
            def get_security_count(self, market):
                return 2 if market == 1 else 0

        cases = (
            (
                ShortDirectoryApi(
                    {0: [], 1: [{"code": "600519", "name": "贵州茅台"}]},
                    {},
                ),
                "DIRECTORY_INCOMPLETE",
            ),
            (
                FakeApi(
                    {0: [], 1: [{"code": "600519", "name": "贵州茅台"}]},
                    {},
                ),
                "QUOTE_INCOMPLETE",
            ),
        )
        for fake, error_code in cases:
            with self.subTest(error_code=error_code):
                outcome = self.provider(fake).call(
                    "review_sentiment",
                    {"query": "沪市A股 非ST", "limit": 20},
                )
                self.assertEqual("provider_error", outcome.status)
                self.assertEqual(error_code, outcome.error_code)
                self.assertIsNone(outcome.data)

    def test_malformed_duplicate_and_zero_close_quotes_fail_closed(self):
        directory = {0: [], 1: [{"code": "600519", "name": "贵州茅台"}]}

        class DuplicateApi(FakeApi):
            def get_security_quotes(self, batch):
                row = quote("600519", 10, 9)
                return [row, row]

        for fake in (
            FakeApi(directory, {(1, "600519"): {"code": "600519", "price": "bad", "last_close": 9}}),
            FakeApi(directory, {(1, "600519"): quote("600519", 10, 0)}),
            DuplicateApi(directory, {}),
        ):
            with self.subTest(fake=type(fake).__name__):
                outcome = self.provider(fake).call(
                    "review_sentiment",
                    {"query": "沪市A股 非ST", "limit": 20},
                )
                self.assertEqual("provider_error", outcome.status)
                self.assertEqual("INVALID_QUOTE", outcome.error_code)

    def test_no_auth_connection_failure_is_auditable(self):
        fake = FakeApi({0: [], 1: []}, {}, connects=False)

        outcome = self.provider(fake).call(
            "review_sentiment", {"query": "沪市A股 非ST", "limit": 20}
        )

        self.assertEqual("network_error", outcome.status)
        self.assertEqual("PYTDX_CONNECT_FAILED", outcome.error_code)
        self.assertEqual({"required": False, "status": "not_required"}, outcome.auth)

    def test_provider_never_uses_legacy_fetch_or_http_fallbacks(self):
        source = inspect.getsource(PytdxScreenerProvider)
        for forbidden in (
            "fetch_quotes",
            "_fallback",
            "tencent",
            "eastmoney",
            "sina",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, source.lower())


class RoutingAndContractTests(unittest.TestCase):
    def setUp(self):
        self.state = ProviderState(":memory:")
        self.state_patch = patch.object(api, "_STATE", self.state)
        self.state_patch.start()
        self.addCleanup(self.state_patch.stop)

    def test_dynamic_route_adds_fifth_source_only_for_compilable_params(self):
        compatible = route_for(
            "review_sentiment",
            {"query": "沪深A股 非ST 非停牌 最新价>=10", "limit": 20},
        )
        self.assertEqual(
            (
                "iwencai_openapi",
                "pywencai",
                "tdx_screener",
                "wind_screener",
                "pytdx_screener",
            ),
            compatible.providers,
        )
        for params in (
            {"query": "沪深A股 半导体 非ST"},
            {"query": "沪深A股 非ST", "version": "v2"},
            {"query": "沪深A股 非ST", "lang": "English"},
            {"query": "沪深A股 非ST", "expected_row_shape": "sector_rows"},
            {"query": "沪深A股 非ST", "date": "2026-07-30"},
        ):
            with self.subTest(params=params):
                self.assertEqual(4, len(route_for("review_sentiment", params).providers))

    def test_incompatible_query_has_no_phantom_pytdx_attempt(self):
        providers = {
            name: unittest.mock.Mock(
                call=unittest.mock.Mock(
                    return_value=ProviderOutcome(
                        provider=name,
                        status="empty",
                        data={"datas": [], "row_count": 0},
                        auth={"required": name != "pytdx_screener", "status": "ok"},
                    )
                )
            )
            for name in (
                "iwencai_openapi",
                "pywencai",
                "tdx_screener",
                "wind_screener",
                "pytdx_screener",
            )
        }
        with patch.object(api, "_provider_for", side_effect=providers.__getitem__):
            result = api.query(
                "review_sentiment", query="沪深A股 半导体 非ST", limit=20
            )

        self.assertEqual("empty", result["_meta"]["status"])
        self.assertEqual("wind_screener", result["_meta"]["provider_used"])
        self.assertNotIn(
            "pytdx_screener",
            [attempt["provider"] for attempt in result["_meta"]["attempts"]],
        )
        providers["pytdx_screener"].call.assert_not_called()

    def test_mixed_error_and_final_pytdx_empty_remains_error(self):
        statuses = {
            "iwencai_openapi": ("auth_error", None, "HTTP_401"),
            "pywencai": ("empty", {"datas": [], "row_count": 0}, None),
            "tdx_screener": ("empty", {"datas": [], "row_count": 0}, None),
            "wind_screener": ("empty", {"datas": [], "row_count": 0}, None),
            "pytdx_screener": ("empty", {"datas": [], "row_count": 0}, None),
        }
        providers = {}
        for name, (status, data, error_code) in statuses.items():
            provider = unittest.mock.Mock()
            provider.call.return_value = ProviderOutcome(
                provider=name,
                status=status,
                data=data,
                error_code=error_code,
                auth={"required": name != "pytdx_screener", "status": "missing" if name == "iwencai_openapi" else "ok"},
            )
            providers[name] = provider
        with patch.object(api, "_provider_for", side_effect=providers.__getitem__):
            result = api.query(
                "review_sentiment",
                query="沪深A股 非ST 非停牌 最新价>=10",
                limit=20,
            )

        self.assertEqual("error", result["_meta"]["status"])
        self.assertIsNone(result["_meta"]["provider_used"])
        self.assertEqual(5, len(result["_meta"]["attempts"]))
        self.assertEqual("missing", result["_meta"]["auth"]["status"])


if __name__ == "__main__":
    unittest.main()
