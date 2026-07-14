#!/usr/bin/env python3
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ym_stock_data.v2.resolve import resolve


def main() -> int:
    now = datetime.now().astimezone()
    snapshot = {
        "queried_at": now.isoformat(),
        "local": {
            "review_sentiment": resolve(
                "review_sentiment",
                query="昨日涨停 今日涨跌幅 非st",
                limit=50,
            ),
            "market_limit_state": resolve("market_limit_state"),
        },
        "manual_tdx_mcp": {
            "status": "not_called",
            "note": "TDX MCP 由 Agent 按验证清单人工填写，不在脚本内自动调用",
        },
    }
    output_dir = Path.home() / ".ym-stock-data" / "compare"
    output_dir.mkdir(parents=True, exist_ok=True)
    output = output_dir / f"{now.date().isoformat()}.json"
    output.write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
