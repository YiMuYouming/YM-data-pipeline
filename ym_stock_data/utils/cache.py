"""本地文件缓存 — CSV/JSON 缓存，带过期时间"""

import json, time
from pathlib import Path


def _ensure_dir(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)


def save_json(path: Path, data, pretty=True):
    _ensure_dir(path)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2 if pretty else None)


def load_json(path: Path, max_age: int = 0):
    """加载 JSON 缓存。max_age=0 不过期"""
    if not path.exists():
        return None
    if max_age > 0 and (time.time() - path.stat().st_mtime) > max_age:
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_csv(path: Path, header: list, rows: list[list]):
    _ensure_dir(path)
    with open(path, "w", encoding="utf-8") as f:
        f.write(",".join(header) + "\n")
        for row in rows:
            f.write(",".join(str(c) for c in row) + "\n")


def load_csv(path: Path, max_age: int = 0):
    if not path.exists():
        return None
    if max_age > 0 and (time.time() - path.stat().st_mtime) > max_age:
        return None
    with open(path, encoding="utf-8") as f:
        lines = f.read().strip().split("\n")
    if not lines:
        return None
    header = lines[0].split(",")
    rows = [line.split(",") for line in lines[1:]]
    return header, rows
