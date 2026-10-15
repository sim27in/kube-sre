"""Upload eval results to LangSmith: the scenarios as a dataset, and a results dir as one experiment per agent.

Nothing is re-run. Each experiment's target returns the stored run for the example, and the evaluator returns
the stored scores, so LangSmith shows exactly what runs.jsonl holds and runs can be compared in its UI.
Needs LANGSMITH_API_KEY.
"""

from collections import defaultdict, deque
from pathlib import Path
from typing import Any

from kube_sre.agent import user_prompt
from kube_sre.evals.runner import RunRecord, load_runs
from kube_sre.evals.scenarios import Scenario
from kube_sre.evals.scoring import RunScore

DATASET = "kube-sre-scenarios"


def example(scenario: Scenario) -> dict[str, Any]:
    return {
        "inputs": {"scenario": scenario.id, "namespace": scenario.namespace, "prompt": user_prompt(scenario.namespace)},
        "outputs": {
            "summary": scenario.summary,
            "incidents": [e.model_dump(mode="json") for e in scenario.incidents],
            "also_ok": scenario.also_ok,
        },
        "metadata": {"scenario": scenario.id},
    }


def sync_dataset(client, scenarios: list[Scenario]) -> None:
    """Create the dataset if needed, add new scenarios and update changed ones."""
    if not client.has_dataset(dataset_name=DATASET):
        client.create_dataset(DATASET, description="kube_sre eval scenarios: namespace to investigate, known answer.")
    existing = {(e.metadata or {}).get("scenario"): e for e in client.list_examples(dataset_name=DATASET)}
    new = []
    for s in scenarios:
        ex = example(s)
        old = existing.get(s.id)
        if old is None:
            new.append(ex)
        elif (old.inputs, old.outputs) != (ex["inputs"], ex["outputs"]):
            client.update_example(old.id, inputs=ex["inputs"], outputs=ex["outputs"], metadata=ex["metadata"])
    if new:
        client.create_examples(dataset_name=DATASET, examples=new)


def stored_output(run: RunRecord) -> dict[str, Any]:
    return {
        "report": run.report,
        "error": run.error,
        "score": run.score.model_dump(mode="json"),
        "tool_calls": run.tool_calls,
        "tokens_in": run.tokens_in,
        "tokens_out": run.tokens_out,
        "seconds": run.seconds,
    }


def stored_scores(outputs: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """LangSmith evaluator: the scores the eval already computed for this run."""
    score = RunScore.model_validate(outputs["score"])
    incidents = score.incidents
    n = len(incidents) or 1
    results = [
        ("passed", score.passed),
        ("detected", sum(i.detected for i in incidents) / n if incidents else 1.0),
        ("category_ok", sum(i.category_ok for i in incidents) / n if incidents else 1.0),
        ("root_cause", sum(i.root_cause for i in incidents) / (2 * n) if incidents else 1.0),
        ("remediation", sum(i.remediation for i in incidents) / (2 * n) if incidents else 1.0),
        ("false_positives", len(score.false_positives)),
        ("leaked_secrets", len(score.leaked_secrets)),
    ]
    comment = " | ".join(i.judge_reasoning for i in incidents if i.judge_reasoning)
    return {"results": [{"key": k, "score": float(v), "comment": comment or None} for k, v in results]}


def export(client, results_dir: Path, scenarios: dict[str, Scenario], agents: list[str] | None = None) -> list[str]:
    """One experiment per agent in the results dir. Returns the experiment names."""
    runs = load_runs(results_dir)
    sync_dataset(client, [scenarios[s] for s in sorted({r.scenario for r in runs})])
    examples = list(client.list_examples(dataset_name=DATASET))
    names = []
    for agent in agents or sorted({r.agent for r in runs}):
        mine = sorted((r for r in runs if r.agent == agent), key=lambda r: r.repeat)
        queues: dict[str, deque[RunRecord]] = defaultdict(deque)
        for r in mine:
            queues[r.scenario].append(r)

        def target(inputs: dict[str, Any], queues=queues) -> dict[str, Any]:
            return stored_output(queues[inputs["scenario"]].popleft())

        data = [e for e in examples if (e.metadata or {}).get("scenario") in queues]
        result = client.evaluate(
            target,
            data=data,
            evaluators=[stored_scores],
            experiment_prefix=f"{results_dir.name}-{agent}",
            description=f"Stored runs from evals/results/{results_dir.name}, agent {agent}.",
            metadata={"agent": agent, "model": mine[0].model, "results_dir": results_dir.name},
            num_repetitions=max(len(q) for q in queues.values()),
            max_concurrency=0,
        )
        names.append(result.experiment_name)
    return names
