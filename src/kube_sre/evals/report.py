"""Summarize eval runs as Markdown tables: one row per metric, one column per agent."""

from collections import defaultdict
from statistics import mean, pstdev

from kube_sre.evals.runner import RunRecord

# USD per 1M tokens (input, output), from the provider's price page on 2026-10-14. The cached-input discount
# isn't applied (cached tokens aren't recorded), so costs are an upper bound.
PRICES = {"gpt-5-mini": (0.25, 2.00), "gpt-5-nano": (0.05, 0.40)}


def _pct(num: int, den: int) -> str:
    return f"{100 * num / den:.0f}% ({num}/{den})" if den else "n/a"


def _per_repeat(runs: list[RunRecord], share) -> str:
    """A metric computed separately for each repeat, so the spread shows how much of a difference is noise."""
    by_repeat: dict[int, list[RunRecord]] = defaultdict(list)
    for r in runs:
        by_repeat[r.repeat].append(r)
    if len(by_repeat) < 2:
        return "n/a (1 repeat)"
    values = [100 * share(by_repeat[k]) for k in sorted(by_repeat)]
    return f"{' / '.join(f'{v:.0f}' for v in values)}% (sd {pstdev(values):.1f})"


def _passed(runs: list[RunRecord]) -> float:
    return sum(r.score.passed for r in runs) / len(runs)


def _root_cause_ok(runs: list[RunRecord]) -> float:
    incidents = [i for r in runs for i in r.score.incidents]
    return sum(i.root_cause == 2 for i in incidents) / len(incidents) if incidents else 0.0


def _cost(runs: list[RunRecord]) -> str:
    prices = [PRICES.get(r.model) for r in runs]
    if None in prices:
        return "n/a (no price for the model)"
    total = sum(r.tokens_in * p[0] + r.tokens_out * p[1] for r, p in zip(runs, prices, strict=True)) / 1e6
    return f"${total / len(runs):.4f}"


def _agent_metrics(runs: list[RunRecord]) -> dict[str, str]:
    incidents = [i for r in runs for i in r.score.incidents]
    detected = [i for i in incidents if i.detected]
    spec = [i.spec_checked for i in incidents if i.spec_checked is not None]
    cited = [r.score for r in runs if r.score.citations is not None]
    total_cited = sum(s.citations for s in cited)
    invalid = sum(len(s.invalid_citations) for s in cited)
    graded = [i for i in detected if not (i.judge_reasoning or "").startswith("JUDGE ERROR")]
    return {
        "Runs completed": _pct(sum(r.score.completed for r in runs), len(runs)),
        "**Runs passed** (all correct, no false positives)": _pct(sum(r.score.passed for r in runs), len(runs)),
        "Runs passed, per repeat": _per_repeat(runs, _passed),
        "Incidents detected": _pct(len(detected), len(incidents)),
        "Reported on the workload (not pod/ReplicaSet)": _pct(sum(i.workload_named for i in detected), len(detected)),
        "Category correct": _pct(sum(i.category_ok for i in incidents), len(incidents)),
        "Root cause fully correct (judge 2/2)": _pct(sum(i.root_cause == 2 for i in incidents), len(incidents)),
        "Root cause fully correct, per repeat": _per_repeat(runs, _root_cause_ok),
        "Root cause mean score (0-2)": f"{mean(i.root_cause for i in incidents):.2f}" if incidents else "n/a",
        "Remediation fixes the cause (judge 2/2)": _pct(sum(i.remediation == 2 for i in incidents), len(incidents)),
        "False positives (total)": str(sum(len(r.score.false_positives) for r in runs)),
        "Runs that leaked a seeded secret": str(sum(bool(r.score.leaked_secrets) for r in runs)),
        "Read the spec when needed": _pct(sum(spec), len(spec)) if spec else "n/a",
        "Invalid citations": _pct(invalid, total_cited) if cited else "n/a (no evidence ids)",
        "Judge errors": str(len(detected) - len(graded)),
        "Tool errors per run (mean)": f"{mean(len(r.tool_errors) for r in runs):.1f}",
        "Tool calls per run (mean)": f"{mean(len(r.tool_calls) for r in runs):.1f}",
        "Tokens per run (mean in / out)": f"{mean(r.tokens_in for r in runs):.0f} / "
        f"{mean(r.tokens_out for r in runs):.0f}",
        "Agent cost per run (mean, no cache discount)": _cost(runs),
        "Seconds per run (mean)": f"{mean(r.seconds for r in runs):.0f}",
    }


def summarize(runs: list[RunRecord]) -> str:
    by_agent: dict[str, list[RunRecord]] = defaultdict(list)
    for r in runs:
        by_agent[r.agent].append(r)
    agents = sorted(by_agent)
    metrics = {a: _agent_metrics(by_agent[a]) for a in agents}
    models = sorted({r.model for r in runs})
    repeats = max(r.repeat for r in runs)

    lines = [
        "# Eval results",
        "",
        f"Model: {', '.join(models)}. Scenarios: {len({r.scenario for r in runs})}. Repeats: {repeats}.",
        "",
        "| Metric | " + " | ".join(agents) + " |",
        "|---|" + "---|" * len(agents),
    ]
    for name in metrics[agents[0]]:
        lines.append(f"| {name} | " + " | ".join(metrics[a][name] for a in agents) + " |")

    lines += ["", "## Per scenario (runs passed, mean root-cause score)", ""]
    lines += ["| Scenario | " + " | ".join(agents) + " |", "|---|" + "---|" * len(agents)]
    cells: dict[tuple[str, str], list[RunRecord]] = defaultdict(list)
    for r in runs:
        cells[(r.scenario, r.agent)].append(r)
    for scenario in sorted({r.scenario for r in runs}):
        row = []
        for a in agents:
            rs = cells[(scenario, a)]
            rcs = [i.root_cause for r in rs for i in r.score.incidents]
            rc = f", rc {mean(rcs):.1f}" if rcs else ""
            row.append(f"{sum(r.score.passed for r in rs)}/{len(rs)}{rc}" if rs else "")
        lines.append(f"| {scenario} | " + " | ".join(row) + " |")

    errors = [r for r in runs if r.error]
    if errors:
        lines += ["", "## Errors", ""] + [f"- {r.agent} {r.scenario} #{r.repeat}: {r.error}" for r in errors]
    return "\n".join(lines) + "\n"
