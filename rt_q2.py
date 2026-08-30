from ym_stock_data import query
import json

OUT = "/Users/yimu/Documents/YM_Capital/Market_Watch/out/redteam-queries/2026-08-19"

def save(name, r):
    with open(f"{OUT}/{name}.json", "w", encoding="utf-8") as f:
        json.dump(r, f, ensure_ascii=False, indent=1)
    meta = r.get("_meta", {})
    q = r.get("data", {}).get("datas", [])
    print(f"{name}: status={meta.get('status')} provider={meta.get('provider_used')} rows={len(q)}")

# Q5 持仓批量（含贵州茅台凑数；问财个股级主力口径）
save("Q5_positions", query("review_sentiment",
    query="002792 300454 002896 600519 涨跌幅 成交额 主力净流入 换手率 收盘价 近5日涨跌幅", limit=10, date="2026-08-19"))

# Q4 排除验证：不碰清单（断板高位票 + 防御首板代表）
save("Q4_excluded", query("review_sentiment",
    query="300684 603089 002820 600127 300911 601011 涨跌幅 成交额 主力净流入 收盘价 换手率", limit=10, date="2026-08-19"))

# Q6 板块资金（个股级 top10；标注语义）
for b in ["农林牧渔", "煤炭", "银行", "石油加工贸易", "通信设备", "机器人概念"]:
    save(f"Q6_fund_{b}", query("review_sentiment", query=f"{b} 主力净流入", limit=10, date="2026-08-19"))

# Q8 趋势候选均线（结构化快照）
save("Q8_snapshot", query("stock_snapshot", codes=["002792","300454","002896","603118","300308","600487","601138","688561","002439","300503","688017","300124","688825","002371","000725","600547","601998","002040","000059","601011"]))
