# DP-02 · 10-16 Bots 数据问题交付

本地候选，待审计。主检出锚点 `ee95566a5ade476fa7ab4fbfd4d5f4e8c27befd1`，
施工分支 `fix/dp02-20261016`，仅修改本 worktree 的管道代码/测试/契约及本目录。
没有 push、部署、重启、Hermes 操作、真实 8088 POST、环境/额度/认证改动。
DP-01 的 N1–N3 仍未实施；本次修现有能力，不新增数据源或改变 provider 顺序。

## 审计入口

| 内容 | 位置 |
|---|---|
| 根因、分类、修复与未解决项 | [逐项报告](逐项报告.md) |
| 跨仓根因与未应用补丁边界 | [跨仓接续](cross-repo-handoff.md) |
| 原始试用副本及 source sha256 | historical/、historical-index.json |
| 用户补充 filings 原样输出绑定 | filings-input-binding.json、historical/intel_600825_filings.json |
| 17 次公共只读查询与安全 transport trace | online-*.json |
| 原始数据离线重放 | historical-replay.json、replay_historical.py |
| 跨仓原函数 / 补丁内存副本对照 | cross-repo-proof.json、handoff-gateway.patch、handoff-bots.patch |
| RED→GREEN | red*.stderr、compare-red-valid.stderr、green-final.stderr |
| 全量测试与逐 ID 比较 | baseline/final*.json、baseline/final.stderr、test-id-comparison.json |
| 主检出保护与静态验证 | 00-baseline.json、main-integrity.json、verification.json、sensitive-scan.json |
| 逐文件封存及最终 HEAD | manifest.json、manifest.sha256、提交后生成的 delivery-receipt.json |

复现入口（仅在确有必要时人工选取 label，文件已存在会拒绝重复；不要重跑整组）：

```sh
cd /Users/yimu/Projects/YM_Capital/_wt/dp02-20261016/YM-data-pipeline
./ym-data doctor --json
/Users/yimu/.cache/uv/ym-stock-data-project-envs/3c46abd0eda9dc8d72e391ba61f77bd1ea486733e9fa3050cd7751fad46dbdde/bin/python docs/evidence/dp02-20261016/reproduce.py <label>
```

`reproduce.py` 只用 `from ym_stock_data import query`；transport wrapper 只观察 canonical
请求，不发额外请求，不记录 token/body/异常原文。StockToday 和 cninfo 的既有只读
协议内部使用 HTTP POST，此处没有业务写入或对真实 8088 发 POST。
online JSON 的 code_phase 是按 label 是否含 fixed 生成的阶段标签，不是源码 SHA：
特别是 filings 为用户补充参数后、代码施工期间的首次查询，不能把它误读成 pristine
主检出重演。真实源码基线由 00-baseline/main-integrity 与 Git 锚定，最终代码由 manifest 绑定。

全量命令为 `python docs/evidence/dp02-20261016/run_tests.py baseline|final`；
离线复核命令为 `python docs/evidence/dp02-20261016/verify_delivery.py`。
审计应先核 delivery receipt 和 manifest；不要先重跑这些会写证据的脚本而覆盖封存结果。
manifest 不自哈希；manifest.sha256 与提交后回执作为外部封存锚点。

最终全量 903 IDs：896 passed、2 baseline errors、5 skips；新增 16 项通过，
新增失败 0、原通过丢失 0、消失 IDs 0。保留两个基线环境/历史 DB 错误，不声称全绿。
原始 stderr/历史副本的空白保留；diff-check 排除这些原始证据与统一 diff 补丁的
格式空白。代码/测试/新文档/JSON 容器均检查，原副本核 hash，补丁单独 apply --check。
交付完成后停下，等审计；两份跨仓 patch 未应用任何目标仓库。
