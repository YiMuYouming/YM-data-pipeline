import json
from ym_stock_data import query
r = query("review_sentiment", query="600403 600396 600386 002580 301205 002716 600664 600613 股票名称 最新涨跌幅", limit=10)
d = r.get("data") or {}
cols = [c.get("key") for c in d.get("columns", [])][:8]
print("cols:", cols)
for row in d.get("datas", [])[:10]:
    print(json.dumps(row, ensure_ascii=False)[:220])
