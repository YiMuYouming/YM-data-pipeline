# TB-06 跨仓清单（只写不改）

依据：inputs/trial-20261016-audit.md；源试用根只读，代码/配置未改其他仓。以下盘中问题来自审计/原始 journal，不将盘后一次成功称为盘中问题已修复。

## 数据管道

| 意图 | 10-16 现象 | 复现参数与证据 |
| --- | --- | --- |
| market_limit_state | 超时或502 | `{"date":"20261016","include_promotion":true}`，`/private/var/folders/99/lddylp394r5dzlphgbxl87_40000gn/T/YM-TB-trial/20261016/turns/turn-20261016-1987df8706ad/record-req-20261016-8129d27d6ee6b4d4.json`；`{"date":"20261016"}`，`/private/var/folders/99/lddylp394r5dzlphgbxl87_40000gn/T/YM-TB-trial/20261016/turns/turn-20261016-463acbd4ac6b/record-req-20261016-447d32e4417811b1.json` |
| market_intraday_state | 11–14秒，涨停列表 industry 为空 | `{}`，`/private/var/folders/99/lddylp394r5dzlphgbxl87_40000gn/T/YM-TB-trial/20261016/turns/turn-20261016-1987df8706ad/record-req-20261016-45b2b7003a8d95bb.json`；`{}`，`/private/var/folders/99/lddylp394r5dzlphgbxl87_40000gn/T/YM-TB-trial/20261016/turns/turn-20261016-463acbd4ac6b/record-req-20261016-42775cfad4ba0449.json` |
| sector_index | data_as_of=null 与部分名称缺失 | `{"names":["出版"]}`，`/private/var/folders/99/lddylp394r5dzlphgbxl87_40000gn/T/YM-TB-trial/20261016/turns/turn-20261016-1987df8706ad/record-req-20261016-76515ad6b7018346.json`；`{"names":["文化传媒","出版传媒"]}`，`/private/var/folders/99/lddylp394r5dzlphgbxl87_40000gn/T/YM-TB-trial/20261016/turns/turn-20261016-1987df8706ad/record-req-20261016-d1f5239af272f7f9.json` |
| index_intraday_compare | INCOMPLETE | `{"trade_date":"20261016"}`，`/private/var/folders/99/lddylp394r5dzlphgbxl87_40000gn/T/YM-TB-trial/20261016/turns/turn-20261016-463acbd4ac6b/record-req-20261016-bb31c6c79e1e3c49.json`；`{"trade_date":"20261016"}`，`/private/var/folders/99/lddylp394r5dzlphgbxl87_40000gn/T/YM-TB-trial/20261016/turns/turn-20261016-4edd92df7231/record-req-20261016-7e46c890ad2b5680.json` |
| stocktoday_data | daily_basic 空 | `{"api_name":"daily_basic","max_rows":1,"params":{"trade_date":"20261015","ts_code":"600825.SH"}}`，`/private/var/folders/99/lddylp394r5dzlphgbxl87_40000gn/T/YM-TB-trial/20261016/turns/turn-20261016-de18273ddeaf/record-req-20261016-159a1f918095e1ed.json` |
| stock_kline | 停牌 stale 缺少原因 | `{"code":"601811","count":60,"period":"daily"}`，`/private/var/folders/99/lddylp394r5dzlphgbxl87_40000gn/T/YM-TB-trial/20261016/turns/turn-20261016-1987df8706ad/record-req-20261016-150d629e53ef26e0.json`；`{"code":"301041","count":60,"period":"daily"}`，`/private/var/folders/99/lddylp394r5dzlphgbxl87_40000gn/T/YM-TB-trial/20261016/turns/turn-20261016-7c914b296e50/record-req-20261016-7ade1986c43fb5fd.json` |
| filings | unavailable | 审计原文该行；原始失败未产生持久化 record，请按 trial-parameter-index.json 同轮结果/源 issues.jsonl 复核，参数不可补造。 |
| industry_flow | 缺盘中数据 | `{"limit":10,"trade_date":"20261015"}`，`/private/var/folders/99/lddylp394r5dzlphgbxl87_40000gn/T/YM-TB-trial/20261016/turns/turn-20261016-4edd92df7231/record-req-20261016-e99e18e99fa6cbc3.json` |
| market_board_strength | 覆盖不足、board_member_coverage_below_floor | 审计原文该行；原始失败未产生持久化 record，请按 trial-parameter-index.json 同轮结果/源 issues.jsonl 复核，参数不可补造。 |

- attempts 缺 latency_ms：跨意图输出契约问题，见源 issues.jsonl 与审计“其他问题归属”；后续对应线程按上述参数核原始 _meta.attempts，不由 Bots 合成延迟。
- index_kline 裸码市场歧义：`codes=["000688","000016"], period=daily,count=5,end_date=20261015` 返回 gap；公共 api.normalize_index_code 除000001外裸码默认SZ。Bots 已允许显式 `.SH/.SZ`，`codes=["000688.SH","000016.SH"]` 成功，见 F5-probes.json；管道是否改变裸码语义交其所有者决定。
- stock_event：当前 EVENTS 与 Bots 运行时枚举一致，intelligence/lockup 只读调用成功。原盘中 invalid_request 的确切参数未留存，不猜测是何 event；见 F5-ownership.md，不转成无证据的管道缺陷。

## 轻舟

- 301041 的 condition_order_missing 与 stop_loss_near 当天始终未 ack/关闭；同一 stop_loss_near 从10-15持续，跌破止损时未创建新实例。是否“跌破升级”属于交易语义待用户决定，非本单实现项。证据：`tests/fixtures/tb06/real-alert-replay.json`（源 journal hash、created_at、instance），源 issues.jsonl，审计 P1 段。这里只移交讨论，不请求变更阈值、自动 ack 或新增权限。
- 8088 的 market_limit_state 502/超时由轻舟线程核 GET 代理耗时与管道失败来源；复现参数同上，不能以 Bots 自选备用源绕过。
- V4 源快照包含两个未处理实例而 B 首次不推，是 Bots F1 设计错误，已在分支修复；不把这项转嫁为轻舟新功能。

## 范围与未决

E3/E4/E5 与未覆盖的盘中段留10-19真实场景验收；本单只补T0 E6/E7。所有问题附原始来源，未做POST/ack、部署、push或交易授权。
