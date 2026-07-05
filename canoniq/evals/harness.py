"""Eval harness — runs gold questions against a generated MetricFlow YAML
and scores accuracy against directly-run gold SQL.

`mf query --metrics {metric} --group-by {dims}` (the real MetricFlow CLI) is
used when available; otherwise this falls back to translating the matched
metric's measure back into raw SQL and running it directly against the
warehouse connection — the same "prefer the real tool, fall back to
something we can actually run" pattern used in the validation loop
(canoniq.validation.loop).
"""

import json
import logging
import re
import shutil
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import yaml
from rich.console import Console
from rich.table import Table

from canoniq.evals.tpcds_gold import GoldQuery

logger = logging.getLogger(__name__)

console = Console()

_SQL_AGG = {
    "sum": "SUM",
    "count": "COUNT",
    "count_distinct": "COUNT",
    "average": "AVG",
    "min": "MIN",
    "max": "MAX",
}

_REF_PATTERN = re.compile(r"^ref\('(.+)'\)$")


@dataclass
class EvalResult:
    question: str
    expected_sql: str
    generated_metric: str | None   # which canoniq metric was used
    result_matches: bool
    error: str | None


def _tokenize(text: str) -> set[str]:
    return {t.lower() for t in re.findall(r"[a-zA-Z0-9]+", text)}


def _metric_bag(metric: dict[str, Any]) -> set[str]:
    bag = _tokenize(metric.get("name", "")) | _tokenize(metric.get("description", ""))
    for synonym in (metric.get("meta") or {}).get("canoniq_synonyms") or []:
        bag |= _tokenize(synonym)
    return bag


def find_closest_metric(question: str, metrics: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Find the closest canoniq metric by (lightweight, token-overlap)
    semantic similarity to the gold question."""
    question_tokens = _tokenize(question)
    if not question_tokens:
        return None

    best: dict[str, Any] | None = None
    best_score = 0.0
    for metric in metrics:
        bag = _metric_bag(metric)
        if not bag:
            continue
        overlap = question_tokens & bag
        union = question_tokens | bag
        score = len(overlap) / len(union) if union else 0.0
        if score > best_score:
            best_score = score
            best = metric

    return best if best_score > 0 else None


def _extract_table_name(model_ref: str) -> str | None:
    match = _REF_PATTERN.match(model_ref)
    return match.group(1) if match else None


def _metric_to_sql_expr(
    metric: dict[str, Any], measures_by_name: dict[str, dict[str, Any]]
) -> str | None:
    if metric.get("type") == "simple":
        measure_name = (metric.get("type_params") or {}).get("measure")
        measure = measures_by_name.get(measure_name or "")
        if measure is None:
            return None
        agg = _SQL_AGG.get(measure.get("agg", ""))
        if agg is None:
            return None
        expr = measure.get("expr", "")
        if expr == "1":
            return "COUNT(*)"
        distinct = "DISTINCT " if measure.get("agg") == "count_distinct" else ""
        return f"{agg}({distinct}{expr})"

    # ratio/derived metrics: best-effort, use the raw expr as emitted.
    return (metric.get("type_params") or {}).get("expr")


def _normalize_rows(rows: list[tuple[Any, ...]]) -> list[tuple[Any, ...]]:
    def _round(value: Any) -> Any:
        return round(value, 2) if isinstance(value, float) else value

    return sorted(tuple(_round(v) for v in row) for row in rows)


def _mf_available() -> bool:
    return shutil.which("mf") is not None


def _run_mf_query(metric_name: str, dimensions: list[str]) -> list[tuple[Any, ...]] | None:
    """Best-effort real `mf query` invocation. Returns None (triggering the
    SQL-translation fallback) if `mf` can't actually run here."""
    cmd = ["mf", "query", "--metrics", metric_name]
    if dimensions:
        cmd += ["--group-by", ",".join(dimensions)]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as e:
        logger.warning("`mf query` failed to run: %s", e)
        return None
    if result.returncode != 0:
        return None
    # `mf query` output parsing is intentionally not implemented here; a
    # real dbt project would be needed to validate the output format.
    return None


def run_eval(
    semantic_model_path: str,
    warehouse_conn: Any,
    gold_queries: list[GoldQuery],
) -> list[EvalResult]:
    """
    For each gold query:
    1. Run the gold SQL directly -> gold_result
    2. Find the closest canoniq metric by semantic similarity
    3. Run: mf query --metrics {metric} --group-by {dims} (or the SQL
       fallback if `mf` isn't available)
    4. Compare results: row count, column values, aggregated totals
    5. Record pass/fail + error if any
    """
    document = yaml.safe_load(Path(semantic_model_path).read_text())
    semantic_model = (document.get("semantic_models") or [{}])[0]
    metrics = document.get("metrics") or []
    measures_by_name = {m["name"]: m for m in semantic_model.get("measures") or []}
    table_name = _extract_table_name(semantic_model.get("model", ""))

    results = []
    for gold in gold_queries:
        try:
            gold_result = _normalize_rows(warehouse_conn.execute(gold.sql).fetchall())
        except Exception as e:
            results.append(
                EvalResult(
                    question=gold.question,
                    expected_sql=gold.sql,
                    generated_metric=None,
                    result_matches=False,
                    error=f"failed to run gold SQL: {e}",
                )
            )
            continue

        metric = find_closest_metric(gold.question, metrics)
        if metric is None:
            results.append(
                EvalResult(
                    question=gold.question,
                    expected_sql=gold.sql,
                    generated_metric=None,
                    result_matches=False,
                    error="no matching canoniq metric found",
                )
            )
            continue

        try:
            generated_result = None
            if _mf_available():
                generated_result = _run_mf_query(metric["name"], gold.expected_dimensions)

            if generated_result is None:
                if table_name is None:
                    raise ValueError(
                        f"could not resolve source table from {semantic_model.get('model')!r}"
                    )
                sql_expr = _metric_to_sql_expr(metric, measures_by_name)
                if sql_expr is None:
                    raise ValueError(f"could not translate metric {metric['name']!r} to SQL")

                # Known v0 limitation: this fallback queries the metric's own
                # table directly and does not join in dimensions that live on
                # a different table (e.g. a time dimension on date_dim for a
                # store_sales metric). MetricFlow YAML doesn't carry join
                # info (see emitters/metricflow.py), so cross-table
                # dimensions fail here with a clear DB error rather than
                # silently returning wrong results.
                dims = gold.expected_dimensions
                select_cols = [*dims, sql_expr]
                generated_sql = f"SELECT {', '.join(select_cols)} FROM {table_name}"
                if dims:
                    generated_sql += f" GROUP BY {', '.join(dims)}"
                generated_result = warehouse_conn.execute(generated_sql).fetchall()

            generated_result = _normalize_rows(generated_result)
        except Exception as e:
            results.append(
                EvalResult(
                    question=gold.question,
                    expected_sql=gold.sql,
                    generated_metric=metric["name"],
                    result_matches=False,
                    error=str(e),
                )
            )
            continue

        matches = gold_result == generated_result
        results.append(
            EvalResult(
                question=gold.question,
                expected_sql=gold.sql,
                generated_metric=metric["name"],
                result_matches=matches,
                error=None if matches else "result mismatch",
            )
        )

    return results


def report_eval_results(
    results: list[EvalResult], output_path: str = "canoniq_output/eval_results.json"
) -> float:
    """Print a results table, save to `output_path`, and return accuracy."""
    table = Table(title="canoniq eval results")
    table.add_column("Question")
    table.add_column("Metric")
    table.add_column("Match")
    table.add_column("Error")
    for result in results:
        table.add_row(
            result.question,
            result.generated_metric or "-",
            "[green]PASS[/green]" if result.result_matches else "[red]FAIL[/red]",
            result.error or "",
        )
    console.print(table)

    passing = sum(1 for r in results if r.result_matches)
    accuracy = passing / len(results) if results else 0.0
    console.print(f"Accuracy: {accuracy:.1%} ({passing}/{len(results)})")

    out_path = Path(output_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(
            {"accuracy": accuracy, "results": [asdict(r) for r in results]},
            indent=2,
        )
    )

    return accuracy
