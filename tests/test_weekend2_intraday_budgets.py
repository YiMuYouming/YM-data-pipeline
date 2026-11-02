"""Partial/retry and independent metadata budgets; all prices here are fixtures."""
import tempfile
import threading
import time
import unittest
from collections import Counter
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from ym_stock_data import intraday_state as state
from ym_stock_data.contracts import TZ_SHANGHAI
from ym_stock_data.providers.base import ProviderOutcome


class Source:
    def __init__(self, failures=None, delay=0, stamp='2026-10-30T10:00:00'):
        self.failures = failures or {}
        self.delay, self.stamp = delay, stamp
        self.calls = Counter()
        self.metadata_started = threading.Event()
        self.codes = dict(zip(state.SNAPSHOT_BATCHES, ['600001.SH', '000001.SZ', '300001.SZ', '920001.BJ']))

    def _request_table(self, name, params, **kwargs):
        key = params.get('ts_code', name)
        self.calls[key] += 1
        if name == 'rt_k':
            if self.calls[key] == 1 and key in self.failures:
                return ProviderOutcome('stocktoday', 'timeout', error_code=self.failures[key])
            time.sleep(self.delay)
            code = self.codes[key]
            rows = [{'ts_code': code, 'name': 'fixture', 'close': 10, 'pre_close': 10,
                     'high': 10, 'vol': 1, 'amount': 10, 'updated_at': self.stamp}]
        elif name == 'stk_limit':
            self.metadata_started.set()
            rows = [{'ts_code': code, 'trade_date': params['trade_date'], 'up_limit': 11, 'down_limit': 9}
                    for code in self.codes.values()]
        else:
            self.metadata_started.set()
            time.sleep(.03)
            rows = []
        return ProviderOutcome('stocktoday', 'success' if rows else 'empty', data={'items': rows},
                               fetched_at=self.stamp + '+08:00')


class IntradayBudgetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        for name, value in [('STK_LIMIT_CACHE', Path(self.temp.name)),
                            ('_previous_ladder', lambda *a: (None, None, None)),
                            ('_sealed_break_risk', lambda *a: {'value': None, 'source_gaps': []})]:
            patcher = patch.object(state, name, value)
            patcher.start(); self.addCleanup(patcher.stop)

    def test_transient_segment_timeout_retries_once_and_keeps_diagnostics(self):
        source = Source({'6*.SH': 'TIMEOUT'})
        result = state.build(source, now=datetime(2026, 10, 30, 10, 1, tzinfo=TZ_SHANGHAI))
        self.assertEqual(2, source.calls['6*.SH'])
        self.assertEqual(100, result['coverage']['pct'])
        self.assertEqual(2, len(result['_stocktoday']['batches'][0]['attempts']))

    def test_empty_segment_retries_only_once(self):
        class Empty(Source):
            def _request_table(self, name, params, **kwargs):
                if name == 'rt_k' and params['ts_code'] == '*.BJ':
                    self.calls['*.BJ'] += 1
                    return ProviderOutcome('stocktoday', 'empty', data={'items': []})
                return super()._request_table(name, params, **kwargs)
        source = Empty()
        result = state.build(source, now=datetime(2026, 10, 30, 10, 1, tzinfo=TZ_SHANGHAI), quote_loader=lambda *a: {})
        self.assertEqual(2, source.calls['*.BJ'])
        self.assertTrue(result['partial'])
        self.assertIn('snapshot_batch_failed:*.BJ', result['source_gaps'])

    def test_metadata_does_not_compete_for_spent_snapshot_budget(self):
        source = Source(delay=.12)
        result = state.build(source, now=datetime(2026, 10, 30, 10, 1, tzinfo=TZ_SHANGHAI), total_budget_sec=.1)
        self.assertTrue(source.metadata_started.is_set())
        self.assertNotIn('limit_metadata_budget_exhausted', result['source_gaps'])
        self.assertEqual('empty', result['_stocktoday']['limit_metadata']['status'])

    def test_close_uses_longer_snapshot_budget_and_reports_completeness(self):
        source = Source(delay=.12, stamp='2026-10-30T15:00:00')
        with patch.object(state, 'SNAPSHOT_BATCH_BUDGET_SEC', .05), \
             patch.object(state, 'CLOSE_SNAPSHOT_BATCH_BUDGET_SEC', .4), \
             patch.object(state, 'CLOSE_TOTAL_BUDGET_SEC', 1):
            result = state.build(source, now=datetime(2026, 10, 30, 15, 0, tzinfo=TZ_SHANGHAI))
        self.assertEqual(100, result['coverage']['pct'])
        self.assertTrue(result['close_snapshot_complete'])
        self.assertEqual(.4, result['budget']['snapshot_batch_budget_sec'])

    def test_failed_snapshot_has_no_late_metadata_cache_write(self):
        class Broken(Source):
            def _request_table(self, name, params, **kwargs):
                if name == 'rt_k':
                    return ProviderOutcome('stocktoday', 'timeout', error_code='TIMEOUT')
                if name == 'stk_limit':
                    time.sleep(.03)
                return super()._request_table(name, params, **kwargs)
        with self.assertRaises(state.IntradayStateError):
            state.build(Broken(), now=datetime(2026, 10, 30, 10, 1, tzinfo=TZ_SHANGHAI))
        time.sleep(.05)
        self.assertFalse(list(Path(self.temp.name).glob('*.json')))

    def test_close_round_spends_remaining_budget_on_fast_failed_segment(self):
        """11-02 15:04 收盘轮：3*.SZ 两次 TIMEOUT 只用 2.3s，20s 预算剩 17s 仍整段缺失。"""

        class Flaky(Source):
            def __init__(self):
                super().__init__(stamp='2026-10-30T15:00:00')
                self.segment_attempts = 0

            def _request_table(self, name, params, **kwargs):
                if name == 'rt_k' and params['ts_code'] == '3*.SZ':
                    self.segment_attempts += 1
                    if self.segment_attempts <= 2:
                        return ProviderOutcome('stocktoday', 'timeout', error_code='TIMEOUT')
                return super()._request_table(name, params, **kwargs)

        source = Flaky()
        with patch.object(state, 'CLOSE_SNAPSHOT_BATCH_BUDGET_SEC', .4), \
             patch.object(state, 'CLOSE_TOTAL_BUDGET_SEC', 1):
            result = state.build(source, now=datetime(2026, 10, 30, 15, 4, 54, tzinfo=TZ_SHANGHAI),
                                 quote_loader=lambda *a: {})
        self.assertEqual(3, source.segment_attempts)
        self.assertEqual(100, result['coverage']['pct'])
        self.assertTrue(result['close_snapshot_complete'])

    def test_close_round_spends_remaining_budget_on_empty_limit_metadata(self):
        """11-02 15:04 收盘轮：limit_list_d 两次空返回只用 1.5s，8s 预算剩 6.5s 仍判空。"""

        class EmptyMeta(Source):
            def __init__(self):
                super().__init__(stamp='2026-10-30T15:00:00')
                self.meta_reads = 0

            def _request_table(self, name, params, **kwargs):
                if name == 'limit_list_d':
                    self.meta_reads += 1
                    if self.meta_reads <= 2:
                        return ProviderOutcome('stocktoday', 'empty', data={'items': []})
                    return ProviderOutcome('stocktoday', 'success', data={'items': [
                        {'ts_code': '600001.SH', 'trade_date': params['trade_date'],
                         'industry': '医药商业', 'first_time': '094957'}]})
                return super()._request_table(name, params, **kwargs)

        source = EmptyMeta()
        with patch.object(state, 'CLOSE_SNAPSHOT_BATCH_BUDGET_SEC', .5), \
             patch.object(state, 'CLOSE_TOTAL_BUDGET_SEC', 2):
            result = state.build(source, now=datetime(2026, 10, 30, 15, 4, 54, tzinfo=TZ_SHANGHAI),
                                 quote_loader=lambda *a: {})
        self.assertEqual(3, source.meta_reads)
        self.assertEqual('success', result['_stocktoday']['limit_metadata']['status'])

    def test_close_round_stops_at_four_fast_failures_and_stays_partial(self):
        """收盘持续快失败：四段尝试封顶，仍如实 partial、不假称收盘完整。"""

        class Down(Source):
            def __init__(self):
                super().__init__(stamp='2026-10-30T15:00:00')
                self.segment_attempts = 0
                self.meta_reads = 0

            def _request_table(self, name, params, **kwargs):
                if name == 'rt_k' and params['ts_code'] == '3*.SZ':
                    self.segment_attempts += 1
                    return ProviderOutcome('stocktoday', 'timeout', error_code='TIMEOUT')
                if name == 'limit_list_d':
                    self.meta_reads += 1
                    return ProviderOutcome('stocktoday', 'empty', data={'items': []})
                return super()._request_table(name, params, **kwargs)

        source = Down()
        with patch.object(state, 'CLOSE_SNAPSHOT_BATCH_BUDGET_SEC', .4), \
             patch.object(state, 'CLOSE_TOTAL_BUDGET_SEC', 1):
            result = state.build(source, now=datetime(2026, 10, 30, 15, 4, 54, tzinfo=TZ_SHANGHAI),
                                 quote_loader=lambda *a: {})
        self.assertEqual(4, source.segment_attempts)
        self.assertEqual(4, source.meta_reads)
        self.assertTrue(result['partial'])
        self.assertFalse(result['close_snapshot_complete'])
        self.assertIn('close_snapshot_incomplete', result['source_gaps'])
        self.assertEqual(4, len(result['_stocktoday']['batches'][2]['attempts']))

    def test_explicit_close_mode_is_bounded_public_parameter(self):
        from ym_stock_data.api import _validate_params
        _validate_params('market_intraday_state', {'snapshot_mode': 'close'})
        with self.assertRaisesRegex(ValueError, 'snapshot_mode only accepts close'):
            _validate_params('market_intraday_state', {'snapshot_mode': 'intraday'})


if __name__ == '__main__':
    unittest.main()
