import unittest
from datetime import datetime

from ym_stock_data import api, intraday_state
from ym_stock_data.providers.base import ProviderOutcome
from ym_stock_data.sources import ths_industry


class SectorTimeFieldsTests(unittest.TestCase):
    def test_http_response_time_does_not_prove_quote_time(self):
        self.assertIsNone(api._data_as_of('sector_index', {},
            {'items': [], 'server_time': '2026-10-30T10:00:00+08:00'}))

    def test_explicit_upstream_quote_time_is_retained(self):
        stamp = '2026-10-30T09:59:59+08:00'
        self.assertEqual(datetime.fromisoformat(stamp),
            api._data_as_of('sector_index', {}, {'data_as_of': stamp}))
        row = {'code': '881175', 'name': '医疗服务', 'quote_time': stamp}
        result = ths_industry._select_industries([row], ['881175'], None)
        self.assertEqual(stamp, result['data_as_of'])

    def test_missing_quote_time_has_a_reason(self):
        result = ths_industry._select_industries([{'code': '881175', 'name': '医疗服务'}], ['881175'], None)
        self.assertIsNone(result['data_as_of'])
        self.assertEqual('DATA_TIME_UNKNOWN', result['data_time_reason_code'])

    def test_limit_metadata_preserves_amount_and_zero_open_count(self):
        class Source:
            def _request_table(self, *args, **kwargs):
                return ProviderOutcome('stocktoday', 'success', data={'items': [
                    {'ts_code': '603131.SH', 'trade_date': '20261030', 'fd_amount': 12345,
                     'open_times': 0, 'industry': '通用设备', 'first_time': '92500'},
                    {'ts_code': '600001.SH', 'trade_date': '20261030'}]})
        result = intraday_state._limit_metadata(Source(), '20261030')
        self.assertEqual(12345, result['603131']['seal_amount'])
        self.assertEqual(0, result['603131']['open_count'])
        self.assertIsNone(result['600001']['seal_amount'])
        self.assertIn('UPSTREAM_FIELD_NOT_PROVIDED:seal_amount', result['600001']['field_reason_codes'])


if __name__ == '__main__':
    unittest.main()
