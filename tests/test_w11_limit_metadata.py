import json
import unittest
from pathlib import Path
from unittest.mock import Mock
from ym_stock_data import intraday_state
from ym_stock_data.providers.base import ProviderOutcome

class LimitMetadataTests(unittest.TestCase):
    def test_real_vendor_metadata_is_dated_and_formatted(self):
        fixture=json.loads((Path(__file__).parent/'fixtures/w11-stocktoday-limit-20261009.json').read_text())
        provider=Mock()
        provider._request_table.return_value=ProviderOutcome('stocktoday','success',data=fixture['data'])
        rows=intraday_state._limit_metadata(provider,'20261009')
        self.assertEqual('化学纤维',rows['000420']['industry'])
        self.assertEqual('13:15:39',rows['000420']['seal_time'])
        self.assertEqual(69,len(rows))

    def test_wrong_day_or_failed_metadata_is_not_used(self):
        provider=Mock()
        provider._request_table.return_value=ProviderOutcome('stocktoday','success',data={'items':[{'ts_code':'000420.SZ','trade_date':'20261008','industry':'化纤','first_time':'093100'}]})
        self.assertEqual({},intraday_state._limit_metadata(provider,'20261009'))
        provider._request_table.return_value=ProviderOutcome('stocktoday','provider_error',data=None)
        self.assertEqual({},intraday_state._limit_metadata(provider,'20261009'))
