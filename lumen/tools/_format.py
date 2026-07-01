"""Small formatting helpers shared by tools (Markdown tables, human sizes)."""

from __future__ import annotations

from collections.abc import Sequence


def human_bytes(num: float) -> str:
    """Render a byte count as a human-friendly size string."""
    step = 1024.0
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(num) < step:
            return f"{num:.0f} {unit}" if unit == "B" else f"{num:.1f} {unit}"
        num /= step
    return f"{num:.1f} PB"


def _cell(value: object, max_width: int) -> str:
    text = "" if value is None else str(value)
    text = text.replace("|", "\\|").replace("\n", " ")
    if len(text) > max_width:
        text = text[: max_width - 1] + "…"
    return text


def md_table(headers: Sequence[object], rows: Sequence[Sequence[object]], max_width: int = 40) -> str:
    """Render a compact GitHub-flavoured Markdown table."""
    head = [_cell(h, max_width) for h in headers]
    body = [[_cell(c, max_width) for c in row] for row in rows]
    lines = [
        "| " + " | ".join(head) + " |",
        "| " + " | ".join("---" for _ in head) + " |",
    ]
    for row in body:
        lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines)


def df_to_markdown(df, max_rows: int = 20, max_width: int = 40) -> str:
    """Render a pandas DataFrame head as a Markdown table with a row-count note."""
    total = len(df)
    view = df.head(max_rows)
    headers = [str(c) for c in view.columns]
    rows = view.astype(object).where(view.notna(), None).values.tolist()
    table = md_table(headers, rows, max_width=max_width)
    if total > max_rows:
        table += f"\n\n_Showing {max_rows} of {total:,} rows._"
    return table


__all__ = ["human_bytes", "md_table", "df_to_markdown"]
