from ym_stock_data import query
import json, os

OUT = "/Users/yimu/Documents/YM_Capital/Market_Watch/out/redteam-queries/2026-08-19"

def save(name, r):
    with open(f"{OUT}/{name}.json", "w", encoding="utf-8") as f:
        json.dump(r, f, ensure_ascii=False, indent=1)
    meta = r.get("_meta", {})
    q = r.get("data", {}).get("datas", [])
    print(f"{name}: status={meta.get('status')} provider={meta.get('provider_used')} rows={len(q)}")

# Q1 热度全景
save("Q1_hot_top50", query("review_sentiment", query="今日热股人气排名前50", limit=50, date="2026-08-19"))

# Q2 板块涨停口径（统一问财概念口径）
for b in ["农林牧渔", "粮食概念", "煤炭概念", "厨卫电器", "石油加工", "港口航运", "银行", "房地产服务", "贵金属", "焦炭加工"]:
    save(f"Q2_zt_{b}", query("review_sentiment", query=f"{b}概念 涨停", limit=30, date="2026-08-19"))

# Q2.5 行业板块榜（语义可能降级个股行）+ 中军
save("Q25_sector_rank", query("review_sentiment", query="今日行业板块 涨幅 成交额 主力净流入 排名前20", limit=20, date="2026-08-19"))
save("Q3_midcap", query("review_sentiment", query="涨幅3%到10% 成交额大于10亿 换手率5%到20% 非涨停", limit=100, date="2026-08-19"))
