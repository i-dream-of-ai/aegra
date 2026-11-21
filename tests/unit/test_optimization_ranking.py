from graphs.ai_trader.optimization_ranking import rank_trials


def test_rank_uses_objective_constraints_and_completed_trials():
    def trial(drawdown, sharpe, progress=1, exit_code=0):
        return {
            "statistics": {"7": drawdown, "15": sharpe},
            "progress": progress,
            "exitCode": exit_code,
        }

    opt = {
        "criterion": {
            "target": "TotalPerformance.PortfolioStatistics.Drawdown",
            "extremum": "min",
        },
        "constraints": [
            {"target": "SharpeRatio", "operator": "GreaterOrEqual", "targetValue": 0}
        ],
        "backtests": {
            "a": trial(10, 1),
            "b": trial("2%", "0"),
            "c": trial(0, -1),
            "d": trial(None, 2),
            "e": trial(0, 1, 0.5),
            "f": trial(0, 1, 1, 1),
        },
    }
    rows, count, _ = rank_trials(opt)
    assert count == 6
    assert [row["backtestId"] for row in rows] == ["b", "a"]
    assert rows[0]["statistics"]["Sharpe Ratio"] == 0

    opt["criterion"] = {"target": "SharpeRatio", "extremum": "max"}
    opt["constraints"] = [
        {"target": "Drawdown", "operator": "Less", "targetValue": 0.05}
    ]
    rows, _, _ = rank_trials(opt)
    assert [row["backtestId"] for row in rows] == ["b", "c"]
