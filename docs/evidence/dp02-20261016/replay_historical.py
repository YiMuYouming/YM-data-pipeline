"""Replay exact captured public data offline; no shared state or network."""
from datetime import datetime
import json
from pathlib import Path
import sys
import tempfile
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from ym_stock_data import api, query
from ym_stock_data.provider_policy import CompiledPolicy
from ym_stock_data.provider_state import ProviderState
from ym_stock_data.providers.base import ProviderOutcome

def envelope(value):
    if isinstance(value, dict):
        if "data" in value and "_meta" in value:
            return value
        for child in value.values():
            found = envelope(child)
            if found:
                return found
    return None

def main():
    results = []
    for label in ("sector_index-1", "stock_kline-2", "index_intraday_compare-1"):
        record = json.loads(Path(__file__).with_name("historical").joinpath(label + "-record.json").read_text())
        payload = json.loads(Path(__file__).with_name("historical").joinpath(label + "-payload.json").read_text())
        captured = envelope(payload)
        params = payload.get("params") or record["params"]
        old = captured["_meta"]
        outcome = ProviderOutcome(old["provider_used"], "success", data=captured["data"], fetched_at=old["fetched_at"], latency_ms=1)
        with tempfile.TemporaryDirectory() as temp:
            state = ProviderState(Path(temp) / "state.sqlite")
            with patch.object(api, "_provider_for", return_value=Mock(call=Mock(return_value=outcome))), \
                 patch.object(api, "_provider_state", return_value=state), \
                 patch.object(api, "load_compiled_policy", return_value=CompiledPolicy(None, "inactive", None)), \
                 patch.object(api, "_now_shanghai", return_value=datetime.fromisoformat(old["fetched_at"])):
                result = query(record["intent"], **params)
        results.append({"capture": label, "intent": record["intent"], "requested_params": record["params"], "effective_params": params, "before_meta": old, "after": result})
    sector, bars, compare = [item["after"]["_meta"] for item in results]
    assert sector["data_as_of"] is None and "DATA_TIME_UNKNOWN" in sector["quality"]["reason_codes"]
    assert bars["freshness"]["status"] == "stale" and "BAR_TRADE_DATE_LAG" in bars["quality"]["reason_codes"]
    assert compare["data_as_of"] is not None
    Path(__file__).with_name("historical-replay.json").write_text(json.dumps({"mode": "offline exact original data; latencies synthetic 1ms", "results": results, "passed": True}, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({item["capture"]: item["after"]["_meta"] for item in results}, ensure_ascii=False))

if __name__ == "__main__":
    main()
