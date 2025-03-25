from __future__ import annotations

import html


def display_name(
    first_name: str | None,
    last_name: str | None,
    username: str | None,
    none_text: str = "(no name)",
) -> str:
    fn = (first_name or "").strip()
    ln = (last_name or "").strip()
    un = (username or "").strip()

    if fn or ln:
        name = (fn + (" " + ln if ln else "")).strip()
        if un:
            return f"{html.escape(name)} (@{html.escape(un)})"
        return html.escape(name)
    if un:
        return f"@{html.escape(un)}"
    return none_text
