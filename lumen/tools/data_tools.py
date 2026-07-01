"""Data-processing tools: inspect, summarise, query, aggregate, chart, clean, convert.

All file access goes through the workspace sandbox. Inputs may be CSV/TSV, Excel,
JSON, or Parquet; outputs (charts, cleaned data) are written to the workspace
output directory.
"""

from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")  # headless: we run inside the server thread, no display
import matplotlib.pyplot as plt  # noqa: E402
import pandas as pd  # noqa: E402

from ..logging_setup import get_logger  # noqa: E402
from ..workspace import workspace  # noqa: E402
from ._format import df_to_markdown, human_bytes, md_table  # noqa: E402
from .registry import register_tool  # noqa: E402

logger = get_logger(__name__)

_CHART_KINDS = {"bar", "barh", "line", "scatter", "hist", "pie", "box", "area"}


def _load(path: str, sheet: str | None = None) -> tuple[pd.DataFrame, Path]:
    """Load a tabular file into a DataFrame, dispatching on extension."""
    resolved = workspace.resolve_read(path)
    if not resolved.exists():
        raise FileNotFoundError(workspace.display(resolved))
    suffix = resolved.suffix.lower()
    if suffix in {".csv", ".txt"}:
        return pd.read_csv(resolved), resolved
    if suffix == ".tsv":
        return pd.read_csv(resolved, sep="\t"), resolved
    if suffix in {".xlsx", ".xls"}:
        return pd.read_excel(resolved, sheet_name=sheet or 0), resolved
    if suffix == ".json":
        return pd.read_json(resolved), resolved
    if suffix == ".parquet":
        return pd.read_parquet(resolved), resolved
    raise ValueError(
        f"Unsupported file type '{suffix}'. Supported: .csv, .tsv, .xlsx, .json, .parquet"
    )


def _require_columns(df: pd.DataFrame, columns: list[str]) -> None:
    missing = [c for c in columns if c not in df.columns]
    if missing:
        raise KeyError(
            f"Column(s) {missing} not found. Available columns: {list(df.columns)}"
        )


def inspect_dataset(path: str, sheet: str | None = None, max_rows: int = 5) -> str:
    """Inspect a dataset: its shape, columns, dtypes, missing values, and a preview.

    Use this first when the user points you at a data file so you understand its
    structure before doing anything else.

    Args:
        path: Path to a .csv, .tsv, .xlsx, .json, or .parquet file.
        sheet: For Excel files, the sheet name to read (defaults to the first sheet).
        max_rows: How many example rows to preview.
    """
    df, resolved = _load(path, sheet)
    rows, cols = df.shape
    schema_rows = [
        [str(c), str(df[c].dtype), f"{int(df[c].isna().sum()):,}"] for c in df.columns
    ]
    schema = md_table(["column", "dtype", "missing"], schema_rows)
    preview = df_to_markdown(df, max_rows=max_rows)
    return (
        f"**{workspace.display(resolved)}** — {rows:,} rows × {cols} columns\n\n"
        f"**Columns**\n{schema}\n\n**Preview**\n{preview}"
    )


def summarize_dataset(path: str, sheet: str | None = None) -> str:
    """Produce summary statistics: numeric describe() plus top categories per text column.

    Args:
        path: Path to the dataset.
        sheet: Excel sheet name, if applicable.
    """
    df, resolved = _load(path, sheet)
    out = [f"**Summary of {workspace.display(resolved)}** ({len(df):,} rows)"]

    numeric = df.select_dtypes(include="number")
    if not numeric.empty:
        desc = numeric.describe().round(3).transpose()
        desc.insert(0, "metric", desc.index)
        out.append("\n**Numeric columns**\n" + df_to_markdown(desc, max_rows=30))

    categorical = df.select_dtypes(exclude="number")
    for col in categorical.columns[:6]:
        top = df[col].value_counts().head(5)
        rows = [[str(idx), f"{int(val):,}"] for idx, val in top.items()]
        out.append(f"\n**{col}** (top values)\n" + md_table([col, "count"], rows))

    if numeric.empty and categorical.empty:
        return "The dataset has no columns to summarise."
    return "\n".join(out)


def query_dataset(path: str, expression: str, sheet: str | None = None, limit: int = 20) -> str:
    """Filter rows with a pandas query expression and return the matches.

    Example expressions: ``"price > 100"``, ``"country == 'US' and sales > 0"``.

    Args:
        path: Path to the dataset.
        expression: A pandas ``DataFrame.query`` expression.
        sheet: Excel sheet name, if applicable.
        limit: Maximum number of matching rows to return.
    """
    df, _ = _load(path, sheet)
    try:
        result = df.query(expression)
    except Exception as exc:  # noqa: BLE001 - surface pandas' message to the agent
        raise ValueError(
            f"Could not evaluate query {expression!r}: {exc}. Columns: {list(df.columns)}"
        ) from exc
    if result.empty:
        return f"No rows match `{expression}`."
    return f"**{len(result):,} rows match** `{expression}`\n\n" + df_to_markdown(result, max_rows=limit)


def aggregate_dataset(
    path: str,
    group_by: list[str],
    value_column: str,
    agg: str = "sum",
    sheet: str | None = None,
) -> str:
    """Group rows and aggregate a value column (sum, mean, count, min, max, median).

    Args:
        path: Path to the dataset.
        group_by: One or more columns to group by.
        value_column: The numeric column to aggregate.
        agg: Aggregation function: sum, mean, count, min, max, or median.
        sheet: Excel sheet name, if applicable.
    """
    df, _ = _load(path, sheet)
    _require_columns(df, [*group_by, value_column])
    allowed = {"sum", "mean", "count", "min", "max", "median"}
    if agg not in allowed:
        raise ValueError(f"agg must be one of {sorted(allowed)}, got {agg!r}")
    grouped = (
        df.groupby(group_by)[value_column].agg(agg).reset_index().sort_values(value_column, ascending=False)
    )
    return (
        f"**{agg}({value_column}) by {', '.join(group_by)}**\n\n"
        + df_to_markdown(grouped, max_rows=30)
    )


def chart_dataset(
    path: str,
    kind: str,
    x: str,
    y: str | None = None,
    title: str | None = None,
    sheet: str | None = None,
    output_name: str | None = None,
) -> str:
    """Create a chart from a dataset and save it as a PNG in the workspace.

    Args:
        path: Path to the dataset.
        kind: One of bar, barh, line, scatter, hist, pie, box, area.
        x: Column for the x-axis (or the category/labels column for pie).
        y: Column for the y-axis (not needed for hist; optional for pie).
        title: Optional chart title.
        sheet: Excel sheet name, if applicable.
        output_name: Optional output filename (defaults to a name derived from the chart).
    """
    if kind not in _CHART_KINDS:
        raise ValueError(f"kind must be one of {sorted(_CHART_KINDS)}, got {kind!r}")
    df, _ = _load(path, sheet)
    needed = [x] + ([y] if y else [])
    _require_columns(df, needed)

    plt.style.use("seaborn-v0_8-whitegrid")
    fig, ax = plt.subplots(figsize=(9, 5.2), dpi=150)
    accent = "#3b5bdb"

    if kind == "hist":
        ax.hist(df[x].dropna(), bins=24, color=accent, edgecolor="white")
        ax.set_xlabel(x)
        ax.set_ylabel("frequency")
    elif kind == "scatter":
        ax.scatter(df[x], df[y], color=accent, alpha=0.7, edgecolor="white")
        ax.set_xlabel(x)
        ax.set_ylabel(y or "")
    elif kind == "pie":
        series = df.groupby(x)[y].sum() if y else df[x].value_counts()
        ax.pie(series.values, labels=[str(i) for i in series.index], autopct="%1.1f%%")
    elif kind == "box":
        df.boxplot(column=y or x, by=x if y else None, ax=ax)
        fig.suptitle("")
    else:  # bar, barh, line, area
        data = df.set_index(x)[y] if y else df.set_index(x).iloc[:, 0]
        data.plot(kind=kind, ax=ax, color=accent)
        ax.set_ylabel(y or "")

    ax.set_title(title or f"{kind.title()} chart", fontsize=13, fontweight="bold", pad=12)
    fig.tight_layout()
    name = output_name or f"charts/{kind}-{x}{('-' + y) if y else ''}.png".replace(" ", "_")
    if not name.lower().endswith(".png"):
        name += ".png"
    target = workspace.unique_output(name)
    fig.savefig(target, bbox_inches="tight")
    plt.close(fig)
    return f"📊 Chart saved to **{workspace.display(target)}** ({human_bytes(target.stat().st_size)})."


def clean_dataset(
    path: str,
    drop_duplicates: bool = True,
    dropna: str = "none",
    output_name: str | None = None,
    sheet: str | None = None,
) -> str:
    """Clean a dataset (drop duplicate rows and/or missing values) and save a copy.

    Args:
        path: Path to the dataset.
        drop_duplicates: Remove fully duplicate rows.
        dropna: How to handle missing values: 'none', 'rows' (drop rows with any NaN),
            or 'columns' (drop columns with any NaN).
        output_name: Optional output filename (defaults to <name>-clean.csv).
        sheet: Excel sheet name, if applicable.
    """
    df, resolved = _load(path, sheet)
    before = df.shape
    if drop_duplicates:
        df = df.drop_duplicates()
    if dropna == "rows":
        df = df.dropna(axis=0)
    elif dropna == "columns":
        df = df.dropna(axis=1)
    elif dropna != "none":
        raise ValueError("dropna must be 'none', 'rows', or 'columns'.")

    name = output_name or f"{resolved.stem}-clean.csv"
    if not name.lower().endswith((".csv", ".xlsx")):
        name += ".csv"
    target = workspace.unique_output(name)
    if target.suffix.lower() == ".xlsx":
        df.to_excel(target, index=False)
    else:
        df.to_csv(target, index=False)
    return (
        f"🧹 Cleaned {before[0]:,}×{before[1]} → {df.shape[0]:,}×{df.shape[1]}. "
        f"Saved to **{workspace.display(target)}**."
    )


def convert_dataset(
    path: str, to: str, output_name: str | None = None, sheet: str | None = None
) -> str:
    """Convert a dataset to another format (csv, xlsx, json, parquet).

    Args:
        path: Path to the source dataset.
        to: Target format: 'csv', 'xlsx', 'json', or 'parquet'.
        output_name: Optional output filename.
        sheet: Excel sheet name, if applicable.
    """
    fmt = to.lower().lstrip(".")
    if fmt not in {"csv", "xlsx", "json", "parquet"}:
        raise ValueError("`to` must be one of: csv, xlsx, json, parquet.")
    df, resolved = _load(path, sheet)
    target = workspace.unique_output(output_name or f"{resolved.stem}.{fmt}")
    if fmt == "csv":
        df.to_csv(target, index=False)
    elif fmt == "xlsx":
        df.to_excel(target, index=False)
    elif fmt == "json":
        df.to_json(target, orient="records", indent=2)
    else:
        df.to_parquet(target)
    return f"🔄 Converted to {fmt}. Saved to **{workspace.display(target)}**."


# -- registration -----------------------------------------------------------
for _fn in (
    inspect_dataset,
    summarize_dataset,
    query_dataset,
    aggregate_dataset,
    chart_dataset,
    clean_dataset,
    convert_dataset,
):
    register_tool(_fn, category="skills")
