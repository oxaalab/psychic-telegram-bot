from __future__ import annotations

from collections.abc import Iterable
from importlib import resources
from typing import Any

import yaml

_LOCALES: dict[str, dict[str, Any]] = {}
_LANG_META: dict[str, str] = {}
_DEFAULT_LANG = "en"


def _deep_get(d: dict[str, Any], path: str) -> Any | None:
    cur: Any = d
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def reload_locales() -> None:
    """
    (Re)load all YAML files from package directory i18n/locales/.
    """
    global _LOCALES, _LANG_META
    _LOCALES = {}
    _LANG_META = {}

    try:
        loc_dir = resources.files(__package__).joinpath("locales")
        for entry in loc_dir.iterdir():
            if not entry.is_file():
                continue
            if entry.suffix.lower() not in (".yaml", ".yml"):
                continue
            data = yaml.safe_load(entry.read_text(encoding="utf-8")) or {}
            meta = data.get("meta", {})
            code = (meta.get("code") or entry.stem[:2]).lower()
            name = meta.get("name") or entry.stem.title()
            strings = data.get("strings", {})
            if isinstance(strings, dict):
                _LOCALES[code] = strings
                _LANG_META[code] = name
    except Exception:
        pass


def available_codes() -> Iterable[str]:
    return list(_LOCALES.keys())


def language_name(code: str) -> str:
    return _LANG_META.get(code, code)


def t(lang: str, key: str, default: str | None = None, **kwargs) -> str:
    """
    Translate by dotted key. Fallback to English, then default, then key.
    """
    lang = (lang or _DEFAULT_LANG).lower()
    v = _deep_get(_LOCALES.get(lang, {}), key)
    if v is None:
        v = _deep_get(_LOCALES.get(_DEFAULT_LANG, {}), key)
    if v is None:
        v = default if default is not None else key
    if isinstance(v, dict):
        return default if default is not None else key
    try:
        return v.format(**kwargs)
    except Exception:
        return v


reload_locales()
