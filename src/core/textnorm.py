from __future__ import annotations

import re
import unicodedata


def _collapse_unicode_spaces(s: str) -> str:
    """
    Replace any Unicode space separators (Zs) with a regular ASCII space.
    """
    return "".join(" " if unicodedata.category(ch) == "Zs" else ch for ch in s)


def _strip_invisibles_and_controls(s: str) -> str:
    """
    Remove invisible format characters (Cf) — e.g., zero-width joiners/space,
    variation selectors, BOM — and control characters (Cc).
    Newlines/tabs are not expected in Telegram names; treat them as whitespace.
    """
    return "".join(ch for ch in s if unicodedata.category(ch) not in {"Cf", "Cc"})


def sanitize_name(v: str | None) -> str:
    """
    Canonicalize human/username fields for stable comparison & storage:

      • NFC normalize (preserve case; we *do not* case-fold).
      • Convert all Unicode space separators to ' '.
      • Strip invisible format/control chars that cause spammy “change” loops.
      • Collapse consecutive whitespace to a single space.
      • Trim leading/trailing whitespace.

    This helps avoid false positive diffs like toggling zero‑width characters
    or exotic spacing, which previously caused repeated announcements.
    """
    if not v:
        return ""
    s = unicodedata.normalize("NFC", v)
    s = _collapse_unicode_spaces(s)
    s = _strip_invisibles_and_controls(s)
    # Treat any remaining whitespace runs uniformly
    s = re.sub(r"\s+", " ", s).strip()
    return s
