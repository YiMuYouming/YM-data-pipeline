"""Mac 只读同步 Hermes 的 market-facts 封存库（审计回复 21 · 断点 B）。

裁定：**Hermes 是唯一封存者，Mac 只读同步。** 理由：Hermes 有 16:15–18:30 的
定时重试，sealed 收盘快照也挂在它的封存成功分支上；Mac 没有定时器，靠人记得刷。

这个模块只做三件事，不做别的：

1. ``seal_status(day)``——只读探针：Hermes 上这一天封存好了没有（先试 8088 的
   ``GET /api/live/market-facts``，不可用就退回 SSH 只读 sqlite 查询）；
2. ``sync_sealed(day)``——等就绪 → 经 SSH 用 sqlite 在线备份取一份只读副本 →
   逐日指纹校验（远程与副本的 run 条数/最新 run 与哈希必须一致）→ 旧库备份 →
   原子替换 Mac 的封存库；
3. ``local_seal_refusal()``——Mac 上直接跑 ``market-facts refresh`` 时的拒绝理由。

任何一步不成立都报 typed 失败并**不替换**目标库：宁可让下游报缺口，也不留下
一个半新半旧、来源不明的库。整库字节流走 ``open_day.py`` 已在用的 SSH 通道，
不新开凭据。
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

DEFAULT_REMOTE = "agentuser@43.132.146.234"
DEFAULT_REMOTE_DB = "/home/agentuser/YM-data-pipeline/data/market-facts.sqlite3"
DEFAULT_BASE_URL = "http://127.0.0.1:8088"
DEFAULT_DB = Path(__file__).resolve().parents[1] / "data" / "market-facts.sqlite3"
LOCAL_SEAL_ENV = "YM_ALLOW_LOCAL_MARKET_FACTS_SEAL"

FINGERPRINT_SQL = """
SELECT trade_date, COUNT(*), MAX(id) FROM {table} WHERE trade_date=? GROUP BY trade_date
"""


def _fingerprint(conn: sqlite3.Connection, day: str) -> dict[str, Any]:
    """一天的封存指纹：两侧用同一段代码算，才谈得上"拉完校验"。"""
    out: dict[str, Any] = {"trade_date": day, "tables": {}}
    for table in ("limit_runs", "daily_runs"):
        row = conn.execute(
            f"SELECT COUNT(*), MAX(id) FROM {table} WHERE trade_date=?", (day,)
        ).fetchone()
        entry: dict[str, Any] = {"runs": int(row[0] or 0), "latest_run_id": row[1]}
        if row[1] is not None:
            sha = conn.execute(
                f"SELECT payload_sha256 FROM {table} WHERE id=?", (row[1],)
            ).fetchone()
            entry["payload_sha256"] = sha[0] if sha else None
        out["tables"][table] = entry
    return out


def _fingerprint_local(db: Path | str, day: str) -> dict[str, Any]:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return _fingerprint(conn, day)
    finally:
        conn.close()


_REMOTE_FINGERPRINT = '''
import json, sqlite3, sys
db, day = sys.argv[1], sys.argv[2]
conn = sqlite3.connect("file:%s?mode=ro" % db, uri=True)
out = {"trade_date": day, "tables": {}}
for table in ("limit_runs", "daily_runs"):
    row = conn.execute("SELECT COUNT(*), MAX(id) FROM %s WHERE trade_date=?" % table, (day,)).fetchone()
    entry = {"runs": int(row[0] or 0), "latest_run_id": row[1]}
    if row[1] is not None:
        sha = conn.execute("SELECT payload_sha256 FROM %s WHERE id=?" % table, (row[1],)).fetchone()
        entry["payload_sha256"] = sha[0] if sha else None
    out["tables"][table] = entry
print(json.dumps(out, ensure_ascii=False))
'''


def parse_live_payload(payload: Any, day: str) -> dict[str, Any]:
    """把 ``GET /api/live/market-facts`` 的返回判成就绪/未就绪。

    形状取自生产（2026-10-05 实抓，未就绪时）：
    ``{"data": null, "_meta": {"status": "error", "data_as_of": null,
    "attempts": [{"provider": "market_facts", "status": "provider_error",
    "error_code": "FACT_DAY_MISSING"}]}}``；就绪时 ``data`` 是 ``report()``
    的输出（含 ``trade_date``），``_meta.status == "success"``。
    """
    if not isinstance(payload, dict):
        return {"ready": False, "probe": "http", "reason": "payload_not_object"}
    meta = payload.get("_meta") or {}
    data = payload.get("data") or {}
    trade_date = str(data.get("trade_date") or "") if isinstance(data, dict) else ""
    status = str(meta.get("status") or "")
    codes = [a.get("error_code") for a in (meta.get("attempts") or []) if isinstance(a, dict)]
    if status == "success" and trade_date == day:
        return {"ready": True, "probe": "http", "reason": "sealed",
                "data_as_of": meta.get("data_as_of")}
    return {
        "ready": False, "probe": "http", "reason": "not_sealed",
        "status": status, "served_trade_date": trade_date, "attempt_error_codes": codes,
    }


def http_seal_status(day: str, *, base_url: str | None = None, timeout: float = 8.0) -> dict[str, Any]:
    """只读接口探针：``GET /api/live/market-facts`` 的封存日是否等于目标日。"""
    url = (base_url or os.environ.get("YM_LIVE_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
    try:
        from urllib.request import urlopen

        with urlopen(f"{url}/api/live/market-facts", timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception as exc:  # 网络/JSON 都算"探不到"，交给下一层探针
        return {"ready": False, "probe": "http", "reason": f"unreachable:{type(exc).__name__}"}
    return parse_live_payload(payload, day)


def ssh_seal_status(
    day: str,
    *,
    remote: str = DEFAULT_REMOTE,
    remote_db: str = DEFAULT_REMOTE_DB,
    timeout: float = 30.0,
) -> dict[str, Any]:
    """SSH 只读 sqlite 探针（HTTP 不可用时的兜底，也是"封存就绪"的权威判据）。"""
    script = _REMOTE_FINGERPRINT.replace("sys.argv[1]", repr(remote_db)).replace("sys.argv[2]", repr(day))
    try:
        completed = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", remote, "python3", "-"],
            input=script, capture_output=True, text=True, timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {"ready": False, "probe": "ssh", "reason": f"unreachable:{type(exc).__name__}"}
    if completed.returncode != 0:
        return {"ready": False, "probe": "ssh", "reason": f"ssh_rc:{completed.returncode}",
                "stderr": (completed.stderr or "").strip()[:200]}
    try:
        fp = json.loads(completed.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {"ready": False, "probe": "ssh", "reason": "unparseable"}
    sealed = all(fp["tables"][t]["runs"] > 0 for t in ("limit_runs", "daily_runs"))
    return {"ready": sealed, "probe": "ssh",
            "reason": "sealed" if sealed else "not_sealed", "fingerprint": fp}


def seal_status(
    day: str,
    *,
    base_url: str | None = None,
    remote: str = DEFAULT_REMOTE,
    remote_db: str = DEFAULT_REMOTE_DB,
    prefer_http: bool = True,
) -> dict[str, Any]:
    """就绪判定：先问只读接口；接口连不上才退回 SSH 只读查询。

    接口连得上、但它说这一天没封存，就是明确结论（不再问 SSH）——两个探针问的
    是同一个库，不需要"两种都认"。
    """
    if prefer_http:
        status = http_seal_status(day, base_url=base_url)
        if status.get("ready") or not str(status.get("reason") or "").startswith("unreachable:"):
            return status
    return ssh_seal_status(day, remote=remote, remote_db=remote_db)


def wait_for_seal(
    day: str,
    *,
    probe: Callable[[], dict[str, Any]],
    wait_seconds: float = 0.0,
    poll_seconds: float = 60.0,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """轮询到就绪或超时。``wait_seconds=0`` 只探一次，不阻塞。"""
    deadline = now() + max(wait_seconds, 0.0)
    attempts = 0
    while True:
        attempts += 1
        status = probe()
        if status.get("ready"):
            status["attempts"] = attempts
            return status
        if now() >= deadline:
            status["attempts"] = attempts
            status["timed_out"] = True
            return status
        sleep(min(poll_seconds, max(deadline - now(), 0.0)))


def fetch_remote_bytes(
    *,
    remote: str = DEFAULT_REMOTE,
    remote_db: str = DEFAULT_REMOTE_DB,
    timeout: float = 600.0,
) -> bytes:
    """SSH 上做 sqlite 在线备份后把整库字节流回来（不直接拷正在写的文件）。"""
    script = (
        "set -e\n"
        "python3 - <<'PY'\n"
        "import sqlite3\n"
        f"src = sqlite3.connect('file:{remote_db}?mode=ro', uri=True)\n"
        "dst = sqlite3.connect('/tmp/ym-market-facts-pull.sqlite3')\n"
        "src.backup(dst)\n"
        "dst.close(); src.close()\n"
        "PY\n"
        "cat /tmp/ym-market-facts-pull.sqlite3\n"
        "rm -f /tmp/ym-market-facts-pull.sqlite3\n"
    )
    completed = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", remote, "bash", "-s"],
        input=script, capture_output=True, timeout=timeout,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"remote_backup_failed:{completed.returncode}:{(completed.stderr or b'').decode('utf-8', 'replace')[:200]}"
        )
    return completed.stdout


def sync_sealed(
    day: str,
    *,
    target_db: Path | str = DEFAULT_DB,
    remote: str = DEFAULT_REMOTE,
    remote_db: str = DEFAULT_REMOTE_DB,
    base_url: str | None = None,
    wait_seconds: float = 0.0,
    poll_seconds: float = 60.0,
    probe: Callable[[], dict[str, Any]] | None = None,
    fetch: Callable[[], bytes] | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """等 Hermes 封存 → 拉只读副本 → 校验 → 备份旧库 → 原子替换。"""
    target = Path(target_db)
    receipt: dict[str, Any] = {"trade_date": day, "target_db": str(target), "dry_run": dry_run}
    probe = probe or (lambda: seal_status(day, base_url=base_url, remote=remote, remote_db=remote_db))
    status = wait_for_seal(day, probe=probe, wait_seconds=wait_seconds, poll_seconds=poll_seconds)
    receipt["seal_status"] = status
    if not status.get("ready"):
        receipt["status"] = "not_ready"
        receipt["gap_code"] = "MARKET-FACTS-HERMES-NOT-SEALED-001"
        receipt["message"] = "Hermes 尚未封存该交易日；Mac 不再自行封存，等就绪后再拉"
        return receipt
    remote_fp = status.get("fingerprint") or _remote_fingerprint_via_probe(remote, remote_db, day)
    receipt["remote_fingerprint"] = remote_fp
    if dry_run:
        receipt["status"] = "would_pull"
        return receipt

    fetch = fetch or (lambda: fetch_remote_bytes(remote=remote, remote_db=remote_db))
    blob = fetch()
    staging = target.with_suffix(target.suffix + ".incoming")
    backup = target.with_suffix(target.suffix + f".bak-{datetime.now().strftime('%Y%m%dT%H%M%S')}")
    try:
        staging.write_bytes(blob)
        try:
            local_fp = _fingerprint_local(staging, day)
        except sqlite3.DatabaseError as exc:
            receipt["status"] = "verify_failed"
            receipt["gap_code"] = "MARKET-FACTS-PULL-NOT-SQLITE-001"
            receipt["message"] = f"拉回来的副本打不开：{type(exc).__name__}"
            return receipt
        receipt["local_fingerprint"] = local_fp
        if remote_fp and local_fp != remote_fp:
            receipt["status"] = "verify_failed"
            receipt["gap_code"] = "MARKET-FACTS-PULL-FINGERPRINT-MISMATCH-001"
            receipt["message"] = "副本与 Hermes 的封存指纹不一致，不替换本地库"
            return receipt
        if target.is_file():
            shutil.copy2(target, backup)
            receipt["backup"] = str(backup)
        os.replace(staging, target)
    finally:
        if staging.exists():
            staging.unlink()
    receipt["status"] = "pulled"
    receipt["target_fingerprint"] = _fingerprint_local(target, day)
    return receipt


def _remote_fingerprint_via_probe(remote: str, remote_db: str, day: str) -> dict[str, Any] | None:
    status = ssh_seal_status(day, remote=remote, remote_db=remote_db)
    return status.get("fingerprint")


def local_seal_refusal(command: str, *, allow: bool) -> str | None:
    """Mac 上直接封存的拒绝理由；返回 None 表示放行。"""
    if allow or os.environ.get(LOCAL_SEAL_ENV) == "1":
        return None
    if sys.platform != "darwin":
        return None
    return (
        f"market-facts {command} 在 Mac 上默认拒绝执行：Hermes 是涨停事实的唯一封存者"
        "（审计回复 21 · 断点 B）。请改用 `./ym-data market-facts sync-sealed --date YYYYMMDD`"
        "（等 Hermes 封存就绪 → 拉一份只读副本 → 校验）。"
        f"只给测试/回放用时可加 --allow-local-seal 或设 {LOCAL_SEAL_ENV}=1。"
    )
