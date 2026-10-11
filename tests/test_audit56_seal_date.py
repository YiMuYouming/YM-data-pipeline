import unittest
from unittest.mock import patch
from ym_stock_data import market_facts_sync as sync


class SealDateTest(unittest.TestCase):
    def probe(self, payload):
        http = sync.parse_live_payload(payload, "20261008")
        with patch.object(sync, "http_seal_status", return_value=http), patch.object(
            sync, "ssh_seal_status", return_value={"ready": True, "probe": "ssh", "trade_date": "20261008"}
        ) as ssh:
            result = sync.seal_status("20261008")
        return result, ssh

    def test_other_day_queries_authoritative_target(self):
        result, ssh = self.probe({"data": {"trade_date": "20261009"}, "_meta": {"status": "success"}})
        self.assertTrue(ssh.called)
        self.assertEqual(ssh.call_args.args, ("20261008",))
        self.assertEqual(result["trade_date"], "20261008")
        self.assertTrue(result["ready"])

    def test_target_day_ready_needs_no_ssh(self):
        result, ssh = self.probe({"data": {"trade_date": "20261008"}, "_meta": {"status": "success"}})
        self.assertTrue(result["ready"])
        self.assertFalse(ssh.called)

    def test_undated_missing_response_does_not_deny_historical_target(self):
        result, ssh = self.probe({"data": None, "_meta": {
            "status": "error", "attempts": [{"error_code": "FACT_DAY_MISSING"}]}})
        self.assertTrue(ssh.called)
        self.assertTrue(result['ready'])

    def test_explicit_target_missing_remains_not_ready(self):
        result, ssh = self.probe({"data": {"trade_date": "20261008"}, "_meta": {
            "status": "error", "attempts": [{"error_code": "FACT_DAY_MISSING"}]}})
        self.assertFalse(result["ready"])
        self.assertFalse(ssh.called)
