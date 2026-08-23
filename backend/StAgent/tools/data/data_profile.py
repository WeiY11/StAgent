"""Data Profile Tool - One-shot dataset profiling for the analysis loop.

The recommended first step on any new dataset: shape, per-column stats,
data-quality warnings, and suggested next analyses. Output is compact and
LLM-friendly so the agent can present a "dataset card" and plan the EDA.
"""

from __future__ import annotations

from typing import Any

import structlog

from StAgent.tools._data import extend_input_schema, resolve_input
from StAgent.tools.base import SyncTool, ToolCategory, ToolContext

logger = structlog.get_logger(__name__)

# Cap unique-value samples / correlation pairs so results stay compact.
_MAX_TOP_VALUES = 5
_MAX_CORR_PAIRS = 10
_HIGH_NULL_RATIO = 0.2
_HIGH_CARDINALITY_RATIO = 0.95


class DataProfileTool(SyncTool):
    """Profile tabular data: shape, column stats, quality flags, suggestions.

    Features:
    - Dataset overview (rows, columns, memory estimate, duplicate rows)
    - Per-column stats: dtype, nulls, unique count, numeric five-number
      summary, top categorical values, inferred datetime range
    - Data-quality warnings (high nulls, constant columns, duplicates,
      mixed types, high cardinality)
    - Strongest numeric correlations
    - Suggested next analyses driven by detected column roles
    """

    name = "data_profile"
    description = (
        "Profile a tabular dataset in one call: shape, per-column statistics "
        "(nulls, uniques, numeric summary, top values, datetime range), "
        "data-quality warnings, strongest correlations, and suggested next "
        "analyses. Run this FIRST on any new CSV/Excel/records data before "
        "cleaning, aggregation, or charting."
    )
    category = ToolCategory.DATA
    version = "1.0.0"
    timeout_sec = 180
    aliases = ["profile", "data_overview", "describe_data"]
    search_hint = "profile describe overview statistics nulls unique correlation quality EDA explore dataset"
    is_concurrency_safe = True
    is_read_only = True
    interrupt_behavior = "cancel"
    max_result_size_chars = 100_000
    path_params = ("source_path",)

    @property
    def parameters(self) -> dict[str, Any]:
        properties: dict[str, Any] = {
            "columns": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Restrict profiling to these columns (default: all).",
            },
            "sample_rows": {
                "type": "integer",
                "description": "Profile at most this many rows (default: 100000).",
                "default": 100_000,
            },
        }
        extend_input_schema(properties)
        return {
            "type": "object",
            "properties": properties,
            "required": [],
            "additionalProperties": False,
        }

    def get_activity_description(self, params: dict[str, Any] | None = None) -> str | None:
        return "Profiling dataset"

    def execute_sync(self, params: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        """Execute dataset profiling.

        Args:
            params: Tool parameters including data source and column filter.
            context: Execution context.

        Returns:
            Dictionary with overview, per-column profiles, warnings,
            correlations, and suggested analyses.
        """
        try:
            import pandas as pd
        except ImportError as e:
            raise RuntimeError("pandas is not installed. Install with: pip install pandas") from e

        data = resolve_input(params, context, required=False)
        if not data:
            return {
                "overview": {"rows": 0, "columns": 0},
                "columns": [],
                "warnings": [{"type": "empty_dataset", "message": "Dataset is empty."}],
                "suggested_analyses": [],
            }

        sample_rows = int(params.get("sample_rows", 100_000))
        df = pd.DataFrame(data)
        sampled = len(df) > sample_rows
        if sampled:
            df = df.head(sample_rows)

        selected = params.get("columns")
        if selected:
            keep = [c for c in selected if c in df.columns]
            missing = [c for c in selected if c not in df.columns]
            df = df[keep]
        else:
            missing = []

        logger.info("Profiling dataset", rows=len(df), columns=len(df.columns))

        warnings: list[dict[str, Any]] = []
        if missing:
            warnings.append({
                "type": "missing_columns",
                "columns": missing,
                "message": f"Requested columns not found: {missing}",
            })

        duplicate_rows = int(df.duplicated().sum())
        overview = {
            "rows": len(df),
            "columns": len(df.columns),
            "duplicate_rows": duplicate_rows,
            "sampled": sampled,
            "memory_bytes": int(df.memory_usage(deep=True).sum()),
        }
        if duplicate_rows:
            warnings.append({
                "type": "duplicate_rows",
                "count": duplicate_rows,
                "message": f"{duplicate_rows} fully duplicated rows — consider data_clean.",
            })

        column_profiles = [
            self._profile_column(df, col, warnings, pd) for col in df.columns
        ]
        correlations = self._top_correlations(df, pd)
        suggestions = self._suggest_analyses(column_profiles, correlations)

        return {
            "overview": overview,
            "columns": column_profiles,
            "warnings": warnings,
            "correlations": correlations,
            "suggested_analyses": suggestions,
        }

    def _profile_column(
        self, df: Any, col_name: str, warnings: list[dict[str, Any]], pd: Any
    ) -> dict[str, Any]:
        """Build a compact profile for one column, appending quality warnings."""
        col = df[col_name]
        non_null = col.dropna()
        null_count = int(col.isna().sum())
        null_ratio = null_count / len(col) if len(col) else 0.0
        unique_count = int(non_null.nunique())

        role = self._infer_role(non_null, unique_count, pd)
        profile: dict[str, Any] = {
            "name": str(col_name),
            "dtype": str(col.dtype),
            "role": role,
            "null_count": null_count,
            "null_ratio": round(null_ratio, 4),
            "unique_count": unique_count,
        }

        if role == "numeric" and not non_null.empty:
            numeric = pd.to_numeric(non_null, errors="coerce").dropna()
            if not numeric.empty:
                profile["stats"] = {
                    "min": float(numeric.min()),
                    "q1": float(numeric.quantile(0.25)),
                    "median": float(numeric.median()),
                    "mean": round(float(numeric.mean()), 6),
                    "q3": float(numeric.quantile(0.75)),
                    "max": float(numeric.max()),
                    "std": round(float(numeric.std()), 6) if len(numeric) > 1 else 0.0,
                }
        elif role == "datetime" and not non_null.empty:
            parsed = pd.to_datetime(non_null, errors="coerce").dropna()
            if not parsed.empty:
                profile["stats"] = {
                    "min": parsed.min().isoformat(),
                    "max": parsed.max().isoformat(),
                }
        elif role in ("categorical", "text") and not non_null.empty:
            top = non_null.astype(str).value_counts().head(_MAX_TOP_VALUES)
            profile["top_values"] = [
                {"value": str(v), "count": int(c)} for v, c in top.items()
            ]

        if null_ratio >= _HIGH_NULL_RATIO:
            warnings.append({
                "type": "high_null_ratio",
                "column": str(col_name),
                "null_ratio": round(null_ratio, 4),
                "message": f"Column '{col_name}' is {null_ratio:.0%} null.",
            })
        if unique_count <= 1 and len(non_null) > 1:
            warnings.append({
                "type": "constant_column",
                "column": str(col_name),
                "message": f"Column '{col_name}' has a single value — carries no signal.",
            })
        if (
            role in ("categorical", "text")
            and len(non_null) > 20
            and unique_count / len(non_null) >= _HIGH_CARDINALITY_RATIO
        ):
            profile["role"] = "identifier"
            warnings.append({
                "type": "high_cardinality",
                "column": str(col_name),
                "message": f"Column '{col_name}' is nearly all-unique — likely an ID, skip aggregation.",
            })

        return profile

    def _infer_role(self, non_null: Any, unique_count: int, pd: Any) -> str:
        """Classify a column as numeric / datetime / boolean / categorical / text."""
        if non_null.empty:
            return "empty"
        if pd.api.types.is_bool_dtype(non_null):
            return "boolean"
        if pd.api.types.is_numeric_dtype(non_null):
            return "numeric"
        if pd.api.types.is_datetime64_any_dtype(non_null):
            return "datetime"

        sample = non_null.head(200)
        numeric = pd.to_numeric(sample, errors="coerce")
        if numeric.notna().mean() > 0.9:
            return "numeric"
        try:
            parsed = pd.to_datetime(sample, errors="coerce", format="mixed")
        except (TypeError, ValueError):
            parsed = pd.Series([pd.NaT] * len(sample))
        if parsed.notna().mean() > 0.9:
            return "datetime"
        if unique_count <= max(20, int(len(non_null) * 0.05)):
            return "categorical"
        return "text"

    def _top_correlations(self, df: Any, pd: Any) -> list[dict[str, Any]]:
        """Return the strongest pairwise Pearson correlations among numeric columns."""
        numeric_df = df.select_dtypes(include="number")
        if numeric_df.shape[1] < 2 or len(numeric_df) < 3:
            return []
        try:
            corr = numeric_df.corr(numeric_only=True)
        except Exception:  # noqa: BLE001
            return []

        pairs: list[dict[str, Any]] = []
        cols = list(corr.columns)
        for i, a in enumerate(cols):
            for b in cols[i + 1:]:
                value = corr.loc[a, b]
                if pd.notna(value):
                    pairs.append({
                        "columns": [str(a), str(b)],
                        "pearson": round(float(value), 4),
                    })
        pairs.sort(key=lambda p: abs(p["pearson"]), reverse=True)
        return pairs[:_MAX_CORR_PAIRS]

    def _suggest_analyses(
        self,
        column_profiles: list[dict[str, Any]],
        correlations: list[dict[str, Any]],
    ) -> list[str]:
        """Derive concrete next-step suggestions from detected column roles."""
        roles: dict[str, list[str]] = {}
        for profile in column_profiles:
            roles.setdefault(profile["role"], []).append(profile["name"])

        numeric = roles.get("numeric", [])
        categorical = roles.get("categorical", []) + roles.get("boolean", [])
        datetime_cols = roles.get("datetime", [])

        suggestions: list[str] = []
        if datetime_cols and numeric:
            suggestions.append(
                f"Trend over time: aggregate '{numeric[0]}' by '{datetime_cols[0]}' "
                "(data_aggregate) and render a line chart."
            )
        if categorical and numeric:
            suggestions.append(
                f"Group comparison: '{numeric[0]}' by '{categorical[0]}' "
                "(data_aggregate) with a bar chart."
            )
        if len(numeric) >= 2:
            top_pair = correlations[0]["columns"] if correlations else numeric[:2]
            suggestions.append(
                f"Relationship: scatter of '{top_pair[0]}' vs '{top_pair[1]}' "
                "(regression_scatter for the fit)."
            )
        if numeric:
            suggestions.append(
                f"Distribution: histogram/boxplot of '{numeric[0]}' to spot outliers."
            )
        if categorical:
            suggestions.append(
                f"Composition: value counts of '{categorical[0]}' as pie/bar."
            )
        return suggestions[:5]
