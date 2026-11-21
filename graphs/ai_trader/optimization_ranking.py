"""Normalize QuantConnect optimization statistics and rank completed trials."""

from math import isfinite
import operator

# The cloud wire format uses percentage points for these display statistics.
METRICS = {
    "SharpeRatio": (15, "Sharpe Ratio"),
    "ProbabilisticSharpeRatio": (13, "Probabilistic Sharpe Ratio"),
    "CompoundingAnnualReturn": (6, "Compounding Annual Return"),
    "Drawdown": (7, "Drawdown"),
    "TotalNetProfit": (12, "Total Net Profit"),
    "TotalNumberOfTrades": (17, "Total Orders"),
    "WinRate": (20, "Win Rate"),
}
ALIASES = {"cagr": "CompoundingAnnualReturn", "maxdrawdown": "Drawdown",
           "netprofit": "TotalNetProfit", "totaltrades": "TotalNumberOfTrades"}
OPERATORS = {"Less": operator.lt, "LessOrEqual": operator.le, "Greater": operator.gt,
             "GreaterOrEqual": operator.ge, "Equals": operator.eq, "NotEqual": operator.ne}


def number(value):
    try:
        n = float(str(value).strip().removesuffix("%"))
        return n if isfinite(n) else None
    except (TypeError, ValueError):
        return None


def metric_name(target):
    key = str(target).split(".")[-1].replace(" ", "").lower()
    return ALIASES.get(key) or next((name for name in METRICS if name.lower() == key), None)


def statistic(stats, metric):
    index, label = METRICS[metric]
    if isinstance(stats, list):
        return number(stats[index]) if index < len(stats) else None
    if isinstance(stats, dict):
        for key in (str(index), index, metric, label):
            if key in stats:
                return number(stats[key])
    return None


def meets_constraint(stats, constraint):
    metric = metric_name(constraint.get("target"))
    compare = OPERATORS.get(constraint.get("operator"))
    threshold = number(constraint.get("targetValue"))
    value = statistic(stats, metric) if metric else None
    if compare is None or value is None or threshold is None:
        return False
    if metric in ("Drawdown", "CompoundingAnnualReturn", "ProbabilisticSharpeRatio"):
        value /= 100  # The target refers to the unformatted LEAN portfolio statistic.
    return compare(value, threshold)


def rank_trials(opt):
    criterion = opt.get("criterion") or {}
    target = criterion.get("target") or opt.get("optimizationTarget")
    direction = criterion.get("extremum") or opt.get("extremum")
    metric = metric_name(target)
    if not metric or direction not in ("min", "max"):
        raise ValueError("Cannot rank trials without a supported objective and min/max direction")
    trials = opt.get("backtests") or []
    if isinstance(trials, dict):
        trials = [dict(row, id=row.get("id") or key) for key, row in trials.items()]
    rows = []
    for trial in trials:
        stats = trial.get("statistics") or {}
        score = statistic(stats, metric)
        if metric in ("SharpeRatio", "ProbabilisticSharpeRatio") and statistic(stats, "TotalNumberOfTrades") == 0:
            continue
        if (score is None or number(trial.get("exitCode")) != 0
                or number(trial.get("progress", 1)) != 1):
            continue
        if not all(meets_constraint(stats, c) for c in (opt.get("constraints") or [])):
            continue
        rows.append({
            "backtestId": trial.get("id"), "name": trial.get("name"),
            "parameterSet": trial.get("parameterSet", trial.get("parameters", {})),
            "score": score, "statistics": {label: statistic(stats, name)
                                           for name, (_, label) in METRICS.items()},
        })
    rows.sort(key=lambda row: row["score"], reverse=direction == "max")
    for rank, row in enumerate(rows, 1):
        row["rank"] = rank
    return rows, len(trials), {"target": target, "extremum": direction}
