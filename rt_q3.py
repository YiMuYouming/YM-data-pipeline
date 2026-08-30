from ym_stock_data import query
import json
OUT = "/Users/yimu/Documents/YM_Capital/Market_Watch/out/redteam-queries/2026-08-19"
def save(name, r):
    with open(f"{OUT}/{name}.json", "w", encoding="utf-8") as f:
        json.dump(r, f, ensure_ascii=False, indent=1)
    meta = r.get("_meta", {})
    q = r.get("data", {}).get("datas") or []
    print(f"{name}: status={meta.get('status')} provider={meta.get('provider_used')} rows={len(q)}")

save("Q25_sector_rank", query("review_sentiment", query="今日行业板块 涨幅 成交额 主力净流入 排名前20", limit=20, date="2026-08-19"))
save("Q3_midcap", query("review_sentiment", query="涨幅3%到10% 成交额大于10亿 换手率5%到20% 非涨停", limit=100, date="2026-08-19"))
save("Q8_snapshot2", query("stock_snapshot", codes=["002792","300454","002896"]))
save("Q8_snapshot3", query("stock_snapshot", codes=["600547","601998","002040","000059","601011","300911"]))
