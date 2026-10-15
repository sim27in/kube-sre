"""Check the judge against reference grades (evals/judge_labels.yaml): grade each labelled incident several
times, then report agreement with the labels and how often the judge gives itself the same grade."""

import asyncio
from collections import Counter
from pathlib import Path

import yaml
from pydantic import BaseModel

from kube_sre.evals.judge import judge_votes
from kube_sre.evals.runner import RESULTS_DIR, load_runs
from kube_sre.evals.scenarios import EVALS_DIR, load_scenario
from kube_sre.evals.scoring import match_incident

LABELS_FILE = EVALS_DIR / "judge_labels.yaml"
FIELDS = ("root_cause", "remediation")


class LabelledIncident(BaseModel):
    run: str
    agent: str
    scenario: str
    repeat: int
    workload: str
    root_cause: int
    remediation: int
    note: str = ""


class Checked(BaseModel):
    item: LabelledIncident
    stored: dict[str, int]  # the grade in the results dir, from the original judging
    grades: dict[str, list[int]]  # this check's repeated grades

    def majority(self, field: str) -> int:
        return Counter(self.grades[field]).most_common(1)[0][0]


def load_labels(path: Path = LABELS_FILE) -> list[LabelledIncident]:
    return [LabelledIncident.model_validate(i) for i in yaml.safe_load(path.read_text())["items"]]


def _find(item: LabelledIncident):
    run = next(
        r
        for r in load_runs(RESULTS_DIR / item.run)
        if (r.agent, r.scenario, r.repeat) == (item.agent, item.scenario, item.repeat)
    )
    expected = next(e for e in load_scenario(item.scenario).incidents if e.workload == item.workload)
    stored = next(i for i in run.score.incidents if i.workload == item.workload)
    return expected, match_incident(expected, run.report["incidents"]), stored


async def check(
    judge_model, items: list[LabelledIncident], times: int, concurrency: int, votes: int = 1
) -> list[Checked]:
    """Grade each item `times` times. With votes > 1, each grading is the combined vote the eval runner uses."""
    sem = asyncio.Semaphore(concurrency)

    async def grade(expected, incident):
        async with sem:
            return await judge_votes(judge_model, expected, incident, votes)

    async def one(item: LabelledIncident) -> Checked:
        expected, incident, stored = _find(item)
        verdicts = await asyncio.gather(*(grade(expected, incident) for _ in range(times)))
        return Checked(
            item=item,
            stored={f: getattr(stored, f) for f in FIELDS},
            grades={f: [getattr(v, f) for v in verdicts] for f in FIELDS},
        )

    return await asyncio.gather(*(one(i) for i in items))


def _pct(num: int, den: int) -> str:
    return f"{100 * num / den:.0f}% ({num}/{den})"


def summarize_check(results: list[Checked], times: int, votes: int = 1) -> str:
    n = len(results)
    lines = [
        "# Judge check",
        "",
        f"{n} labelled incidents, each graded {times} times"
        + (f" (each grading the median of {votes} judge calls)" if votes > 1 else "")
        + f". Agreement uses the majority of the {times} grades.",
        "",
        "| | Root cause | Remediation |",
        "|---|---|---|",
    ]
    rows = {
        "Agrees with the label": lambda r, f: r.majority(f) == getattr(r.item, f),
        "Within one point of the label": lambda r, f: abs(r.majority(f) - getattr(r.item, f)) <= 1,
        "Judge stricter than the label": lambda r, f: r.majority(f) < getattr(r.item, f),
        "Judge more lenient than the label": lambda r, f: r.majority(f) > getattr(r.item, f),
        f"Same grade in all {times} gradings": lambda r, f: len(set(r.grades[f])) == 1,
        "Stored grade agrees with the label": lambda r, f: r.stored[f] == getattr(r.item, f),
    }
    for name, test in rows.items():
        lines.append(f"| {name} | " + " | ".join(_pct(sum(test(r, f) for r in results), n) for f in FIELDS) + " |")

    disagreements = [r for r in results if any(r.majority(f) != getattr(r.item, f) for f in FIELDS)]
    if disagreements:
        lines += ["", "## Disagreements (label vs judge grades)", ""]
        for r in disagreements:
            i = r.item
            grades = ", ".join(f"{f} {getattr(i, f)} vs {r.grades[f]}" for f in FIELDS)
            lines.append(f"- {i.agent} {i.scenario} #{i.repeat} ({i.run}): {grades}. {i.note}")
    return "\n".join(lines) + "\n"
