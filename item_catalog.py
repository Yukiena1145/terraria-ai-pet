"""Local Terraria item catalog, generated from Lang._itemNameCache."""

from __future__ import annotations

import json
from pathlib import Path

_CATALOG: list[dict] | None = None


def all_items() -> list[dict]:
    global _CATALOG
    if _CATALOG is None:
        path = Path(__file__).with_name("item_catalog.json")
        try:
            _CATALOG = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            _CATALOG = []
    return _CATALOG


def find_items(query: str, limit: int = 12) -> list[dict]:
    q = str(query or "").strip().lower()
    if not q:
        return []
    hits = []
    qchars = [ch for ch in q if not ch.isspace()]
    for item in all_items():
        name = str(item.get("name", ""))
        lower = name.lower()
        # 精确/子串，以及“铁剑”命中“铁短剑”这种按字符顺序的模糊匹配。
        if q in lower or q == str(item.get("id")) or all(ch in lower for ch in qchars):
            hits.append(item)
            if len(hits) >= limit:
                break
    return hits
