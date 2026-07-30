# Task 14 每日验收唯一 Runbook

本文是五个交易日 daily acceptance 的唯一 Agent 执行入口。严格 JSON key、允许状态和安全门禁由 `ym_stock_data.acceptance` 拥有；不要手写另一份 schema。每天只执行一次，且仅在 `Asia/Shanghai` 16:10 后执行。

当前新窗口使用 acceptance 1.2、smoke schema 2 与 baseline `five-source-structured-v1`，并严格锁定 11 个固定 case（含 `explicit_structured_screener`）。历史 acceptance 1.1/1.0 与旧 10-case receipt 仍可只读验证，但不能计入这个新五日窗口；不允许手工改写旧 receipt 或 day count。

本流程只保存元数据。禁止打印或保存业务 rows、查询正文、credential、stderr、exception、原始 transport/session；禁止调用底层 source、兼容 V2、手工 TDX/Wind 或交易工具；禁止向端口 8088 发 POST，禁止写 Market_Watch/live-dashboard 的 out、data、cache、runtime，禁止部署、push、券商或交易动作。

## 1. Preflight 与同日去重

把 `YYYY-MM-DD` 替换成当日日期一次；以下变量后续保持不变。任何 `test` 失败都立即停止，不绕过、不重跑 live 命令。

```bash
set -eu
umask 077
acceptance_date=YYYY-MM-DD
pipeline_root=/Users/yimu/Documents/YM_Capital/YM-data-pipeline
acceptance_dir=/Users/yimu/.ym-stock-data/acceptance
smoke_dir=/Users/yimu/.ym-stock-data/smoke
acceptance_target=${acceptance_dir}/${acceptance_date}.json
acceptance_tmp=$(mktemp -d /tmp/ym-data-acceptance-${acceptance_date}.XXXXXX)
chmod 700 "$acceptance_tmp"
cd "$pipeline_root"
test "$(TZ=Asia/Shanghai date +%F)" = "$acceptance_date"
test "$(TZ=Asia/Shanghai date +%H%M)" -ge 1610
test "$(git branch --show-current)" = codex/unified-a-share-data-channel-canonical
test -z "$(git status --porcelain=v1 --untracked-files=no)"
test -z "$(git diff --cached --name-only)"
test ! -e "$acceptance_target"
test "$(find "$acceptance_dir" -maxdepth 1 -type f -name "${acceptance_date}.json" | wc -l | tr -d ' ')" -eq 0
test "$(find "$smoke_dir" -maxdepth 1 -type f -name "${acceptance_date}*" | wc -l | tr -d ' ')" -eq 0
```

若当日已有 acceptance 或 smoke，停止并交给原审计任务判断；不要猜测是否复用，也不要第二次运行 smoke。

为下游 Python 探针固定 canonical 的 checkout 专属外置环境。本机必须使用已经验证的 arm64 uv；缺失就停止，不自行换解释器。

```bash
project_uv=/opt/homebrew/bin/uv
test -x "$project_uv"
project_cache=$($project_uv cache dir)
project_hash=$(printf '%s' "$pipeline_root" | shasum -a 256)
project_hash=${project_hash%% *}
project_env=${project_cache%/}/ym-stock-data-project-envs/$project_hash
test -x "$project_env/bin/python"
```

## 2. 生成单一权威模板

template 只向 stdout 输出 JSON，不联网、不写文件、不调用 provider。它预置 `confirmed=false`、`is_trading_day=false`、probe `status=pending`、`zero_secret_scan="pending"`，因此未填模板无法通过 build。

```bash
./ym-data acceptance template --date "$acceptance_date" > "$acceptance_tmp/template.json"
chmod 600 "$acceptance_tmp/template.json"
UV_PROJECT_ENVIRONMENT="$project_env" "$project_uv" --project "$pipeline_root" run python - "$acceptance_tmp/template.json" "$acceptance_tmp/calendar.json" "$acceptance_tmp/downstream.json" <<'PY'
import json
import os
import sys
from pathlib import Path

template = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
for key, target in (("calendar", Path(sys.argv[2])), ("downstream", Path(sys.argv[3]))):
    target.write_text(json.dumps(template[key], ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(target, 0o600)
PY
```

只修改 template 已给出的值，禁止增加、删除或重命名 key。

## 3. 官方 SSE 交易日确认

用浏览器直接打开上海证券交易所年度休市安排，不使用搜索摘要或第三方日历。当前 2026 年权威页：

<https://www.sse.com.cn/disclosure/dealinstruc/closed/c/c_20251222_10802510.shtml>

人工确认三件事：页面域名是 `www.sse.com.cn`；`acceptance_date` 的 weekday 与模板一致；该日期不在休市区间。三项均成立后才执行下列更新。`basis` 只写这一事实摘要，不复制网页正文。

```bash
UV_PROJECT_ENVIRONMENT="$project_env" "$project_uv" --project "$pipeline_root" run python - "$acceptance_tmp/calendar.json" <<'PY'
import json
import os
import sys
from pathlib import Path

path = Path(sys.argv[1])
value = json.loads(path.read_text(encoding="utf-8"))
value["is_trading_day"] = True
value["confirmed"] = True
value["official_calendar"]["exchange"] = "Shanghai Stock Exchange"
value["official_calendar"]["url"] = "https://www.sse.com.cn/disclosure/dealinstruc/closed/c/c_20251222_10802510.shtml"
value["official_calendar"]["basis"] = "SSE 2026 closure schedule checked; date is not listed as closed"
path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
os.chmod(path, 0o600)
PY
```

## 4. Doctor 一次、smoke 一次、唯一 receipt

Doctor 只运行一次并直接保存脱敏 JSON：

```bash
./ym-data doctor --json > "$acceptance_tmp/doctor.json" 2>/dev/null
chmod 600 "$acceptance_tmp/doctor.json"
```

Smoke 只运行一次。其 CLI stdout 只保存 receipt 路径和 summary；业务行不会进入该文件。`explicit_structured_screener` 由 smoke 通过 canonical registry 直接取得 `pytdx_screener` provider 并执行固定只读小探针，所以前四个来源不能遮蔽第五源是否真正被调用；receipt 仍只保存脱敏状态、attempt、行数和耗时，不保存查询正文或业务行。不要在 runbook 外再补一次结构化查询。

```bash
./ym-data smoke --live > "$acceptance_tmp/smoke-cli.json" 2>/dev/null
chmod 600 "$acceptance_tmp/smoke-cli.json"
UV_PROJECT_ENVIRONMENT="$project_env" "$project_uv" --project "$pipeline_root" run python - "$acceptance_tmp/smoke-cli.json" "$acceptance_date" "$smoke_dir" "$acceptance_tmp/smoke-path.txt" <<'PY'
import json
import os
import stat
import sys
from pathlib import Path

cli = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
date = sys.argv[2]
smoke_dir = Path(sys.argv[3]).resolve()
receipt = Path(cli["receipt"]).resolve()
matches = sorted(smoke_dir.glob(f"{date}*.json"))
if cli.get("status") != "complete" or len(matches) != 1 or receipt != matches[0].resolve():
    raise SystemExit("SMOKE_RECEIPT_NOT_UNIQUE")
if stat.S_IMODE(receipt.stat().st_mode) != 0o600:
    raise SystemExit("SMOKE_RECEIPT_MODE_INVALID")
target = Path(sys.argv[4])
target.write_text(str(receipt) + "\n", encoding="utf-8")
os.chmod(target, 0o600)
PY
smoke_receipt=$(tr -d '\n' < "$acceptance_tmp/smoke-path.txt")
```

若 smoke 命令或唯一性检查失败，停止；不得重试。

## 5. Breaker 脱敏复验

Smoke 的显式 WenCai case 已完成当日首次尝试。本步骤只再执行一次 canonical public `query()`，并立即用 `summarize_query_result()` 丢弃业务 rows；保存的只有 status/provider/attempts/count/error/latency。若 OpenAPI attempt 不是已有 `HTTP_401` breaker，停止审计，不发第二次探针。

```bash
UV_PROJECT_ENVIRONMENT="$project_env" "$project_uv" --project "$pipeline_root" run python - "$acceptance_tmp/breaker.json" > /dev/null 2>/dev/null <<'PY'
import json
import os
import sys
import time
from pathlib import Path
from ym_stock_data import query
from ym_stock_data.smoke import summarize_query_result

started = time.monotonic()
result = query("review_sentiment", query="A股 非ST 涨停", limit=3)
summary = summarize_query_result(result)
summary["latency_ms"] = max(0, int((time.monotonic() - started) * 1000))
attempts = summary.get("attempts", [])
breaker = next((item for item in attempts if item.get("provider") == "iwencai_openapi"), None)
if not breaker or breaker.get("status") != "breaker_open" or breaker.get("error_code") != "HTTP_401":
    raise SystemExit("BREAKER_NOT_CONFIRMED")
path = Path(sys.argv[1])
path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
os.chmod(path, 0o600)
PY
```

## 6. Market_Watch observation-only 探针

只调用 `_default_resolver`，用 `extract_rows` 计数、用 `_effective_meta` 取得真实 nested provenance；rows 只在内存中计数，不打印、不落盘，也不运行 C1.5 写入主程序。

```bash
UV_PROJECT_ENVIRONMENT="$project_env" "$project_uv" --project "$pipeline_root" run python - "$acceptance_tmp/market-watch.json" > /dev/null 2>/dev/null <<'PY'
import json
import os
import sys
from pathlib import Path

market_root = Path("/Users/yimu/Documents/YM_Capital/Market_Watch")
sys.path.insert(0, str(market_root))
from scripts.c15_contract import _effective_meta, extract_rows
from scripts.run_c15_scan import _default_resolver
from ym_stock_data.smoke import summarize_query_result

result = _default_resolver("review_sentiment")
rows = extract_rows(result)
meta = _effective_meta(result)
summary = summarize_query_result({"data": [], "_meta": meta})
quality = meta.get("quality") if isinstance(meta.get("quality"), dict) else {}
value = {
    "status": summary["status"],
    "provider_used": summary["provider_used"],
    "attempts": summary["attempts"],
    "quality_status": quality.get("status", "error"),
    "returned_count": len(rows),
    "observation_only": True,
}
path = Path(sys.argv[1])
path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
os.chmod(path, 0o600)
PY
```

## 7. live-dashboard unified no-save 探针

先取得一次 canonical 结果。若当前默认仍为 `legacy`，只额外调用一次现有 legacy 查询，以规范六位代码集合做同点比较；比较不完全一致立即停止，不能 build。随后把已经取得的结果通过 lambda 交给 unified guard，禁止再次调用 provider。若默认已为 `unified`，使用空 legacy mock，不调用旧链。不运行 collector、poll、snapshot 或任何持久化入口。

```bash
UV_PROJECT_ENVIRONMENT="$project_env" "$project_uv" --project "$pipeline_root" run python - "$acceptance_tmp/live-dashboard.json" > /dev/null 2>/dev/null <<'PY'
import json
import os
import sys
from pathlib import Path

dashboard_root = Path("/Users/yimu/Documents/YM_Capital/live-dashboard")
sys.path.insert(0, str(dashboard_root))
from scripts.ym_data_query import (
    compare_review_results,
    compat_iwencai_query,
    data_api_mode,
    legacy_review_query,
)
from ym_stock_data import query
from ym_stock_data.smoke import summarize_query_result

def empty_legacy(*args, **kwargs):
    return {"datas": []}

canonical_result = query("review_sentiment", query="A股 非ST 涨停", limit=3)
default_mode = data_api_mode({})
legacy_call = empty_legacy
comparison_status = "unified_default_observed"
if default_mode == "legacy":
    legacy_result = legacy_review_query("A股 非ST 涨停", limit=3)
    comparison_status = compare_review_results(canonical_result, legacy_result)
    if comparison_status != "exact_code_set_match":
        raise SystemExit("DASHBOARD_COMPARISON_FAILED")
    legacy_call = lambda *args, **kwargs: legacy_result

result = compat_iwencai_query(
    "A股 非ST 涨停",
    limit=3,
    mode="unified",
    canonical_fn=lambda *args, **kwargs: canonical_result,
    legacy_fn=legacy_call,
)
rows = result.get("datas") if isinstance(result.get("datas"), list) else []
compat = result.get("_ym_data_compat") if isinstance(result.get("_ym_data_compat"), dict) else {}
meta = result.get("_meta") if isinstance(result.get("_meta"), dict) else compat.get("canonical_meta", {})
summary = summarize_query_result({"data": [], "_meta": meta})
value = {
    "status": summary["status"],
    "provider_used": summary["provider_used"],
    "attempts": summary["attempts"],
    "row_count": len(rows),
    "api_mode_tested": "unified",
    "default_api_mode": default_mode,
    "comparison_status": comparison_status,
    "saved": False,
}
path = Path(sys.argv[1])
path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
os.chmod(path, 0o600)
PY
```

## 8. 映射到 downstream 模板

映射是逐对象替换，不做字段猜测：`breaker.json` → `breaker_verification`；`market-watch.json` → `market_watch`；`live-dashboard.json` → `live_dashboard`。`safety` 保持 template 的十项：八项 mutation flag 必须为 `false`，`metadata_only=true`，完成零敏感扫描后才把 `zero_secret_scan` 从 `pending` 改为 `pass`。

```bash
UV_PROJECT_ENVIRONMENT="$project_env" "$project_uv" --project "$pipeline_root" run python - "$acceptance_tmp/downstream.json" "$acceptance_tmp/breaker.json" "$acceptance_tmp/market-watch.json" "$acceptance_tmp/live-dashboard.json" <<'PY'
import json
import os
import sys
from pathlib import Path

downstream_path = Path(sys.argv[1])
value = json.loads(downstream_path.read_text(encoding="utf-8"))
for key, source in zip(
    ("breaker_verification", "market_watch", "live_dashboard"),
    map(Path, sys.argv[2:]),
):
    value[key] = json.loads(source.read_text(encoding="utf-8"))
downstream_path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
os.chmod(downstream_path, 0o600)
PY
```

## 9. Safety 十项与 zero-secret scan

必须保持以下语义：`broker_or_trading_call=false`、`business_or_production_data_write=false`、`business_rows_stored=false`、`credential_values_stored=false`、`deployment=false`、`exception_or_stderr_text_stored=false`、`git_push=false`、`http_8088_post=false`、`metadata_only=true`；只有扫描通过后才允许 `zero_secret_scan="pass"`。

```bash
if rg -n -i -e 'Bearer[[:space:]]+[A-Za-z0-9._~-]{16,}' -e 'sk-[A-Za-z0-9]{20,}' -e 'eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,}' -e '(api[_-]?key|access[_-]?token|refresh[_-]?token|client[_-]?secret)[[:space:]]*[:=][[:space:]]*[A-Za-z0-9+/=_-]{16,}' "$acceptance_tmp" "$smoke_receipt"; then
  exit 1
fi
UV_PROJECT_ENVIRONMENT="$project_env" "$project_uv" --project "$pipeline_root" run python - "$acceptance_tmp/downstream.json" <<'PY'
import json
import os
import sys
from pathlib import Path

path = Path(sys.argv[1])
value = json.loads(path.read_text(encoding="utf-8"))
value["safety"]["zero_secret_scan"] = "pass"
path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
os.chmod(path, 0o600)
PY
```

## 10. Build、validate 与只读自检

先确认 canonical Git 仍干净，再 build 一次。builder 会再次校验严格 key set、交易日、smoke、Git、pyc、日期序列与安全字段；失败时停止，不能手工放宽输入。

```bash
test -z "$(git status --porcelain=v1 --untracked-files=no)"
./ym-data acceptance build \
  --date "$acceptance_date" \
  --doctor "$acceptance_tmp/doctor.json" \
  --smoke "$smoke_receipt" \
  --downstream "$acceptance_tmp/downstream.json" \
  --calendar "$acceptance_tmp/calendar.json"
./ym-data acceptance validate "$acceptance_target"
test "$(stat -f '%Lp' "$acceptance_dir")" = 700
test "$(stat -f '%Lp' "$acceptance_target")" = 600
test "$(find "$acceptance_dir" -maxdepth 1 -type f -name "${acceptance_date}.json" | wc -l | tr -d ' ')" -eq 1
shasum -a 256 "$acceptance_target" "$smoke_receipt"
git status --porcelain=v1 --untracked-files=no
git diff --cached --name-only
git check-ignore -v \
  ym_stock_data/experimental/__pycache__/__init__.cpython-314.pyc \
  ym_stock_data/experimental/__pycache__/wind_sidecar.cpython-314.pyc
shasum -a 256 \
  ym_stock_data/experimental/__pycache__/__init__.cpython-314.pyc \
  ym_stock_data/experimental/__pycache__/wind_sidecar.cpython-314.pyc
```

最后只汇报 receipt 绝对路径、SHA、mode、day_count/status counts/provider attempts 摘要、doctor 状态、下游元数据与未解决风险。不得输出临时 JSON 全文或宣称五日闭环；`day_count=5` 后仍需独立审核。
