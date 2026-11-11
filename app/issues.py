"""Known firmware bugs / model caveats lookup."""
import json
import os
import re

from config import DATA_DIR

_BUILTIN = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "known_issues.json")
_USER = os.path.join(DATA_DIR, "known_issues.json")
_cache = {"key": None, "items": []}


def _load():
    key = tuple(os.path.getmtime(p) if os.path.exists(p) else 0 for p in (_BUILTIN, _USER))
    if _cache["key"] != key:
        items = []
        for p in (_USER, _BUILTIN):
            if os.path.exists(p):
                try:
                    with open(p, encoding="utf-8") as f:
                        items += json.load(f).get("issues", [])
                except (OSError, ValueError):
                    pass
        _cache.update(key=key, items=items)
    return _cache["items"]


def lookup(summary):
    model = re.sub(r"\s+", " ", summary.get("model") or "")
    fw = (summary.get("firmware") or "").strip()
    out, seen = [], set()
    for it in _load():
        if it.get("id") in seen:
            continue
        try:
            if not re.search(it["match"], model, re.I):
                continue
        except re.error:
            continue
        if it.get("firmware") and fw not in it["firmware"]:
            continue
        seen.add(it.get("id"))
        out.append({k: it.get(k) for k in ("id", "severity", "title", "detail", "fix")})
    return out
