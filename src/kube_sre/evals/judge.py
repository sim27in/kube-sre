"""LLM judge: grades a reported root cause and remediation against a scenario's known answer.

Category, workload and citations are checked in code (scoring.py). The judge only grades the free text,
which string matching can't: "32Mi limit vs ~100MB allocation" and "memory limit too low for the
allocation" are the same diagnosis.
"""

from typing import Any, Literal

from langchain_core.language_models import BaseChatModel
from pydantic import BaseModel, Field

from kube_sre.config import get_settings
from kube_sre.evals.scenarios import ExpectedIncident
from kube_sre.llm import get_chat_model

JUDGE_PROMPT = """You grade one Kubernetes incident diagnosis against the known ground truth.

Grade the ROOT CAUSE:
- 2: names the underlying cause and every key fact (specific values such as limits, ports, keys or names
  where the key facts include them). Wording may differ, and details beyond the key facts are not required.
- 1: right area but incomplete: describes the symptom instead of the cause (e.g. "OOMKilled" without why),
  misses a key fact, or hedges between several causes.
- 0: wrong cause, or none.

Grade the REMEDIATION (the proposed actions taken together):
- 2: at least one action would fix the root cause, and none would make things worse.
- 1: generic or partial (e.g. "investigate", "restart the pod", or a fix in the right area with wrong values),
  or a correct fix mixed with a harmful one.
- 0: would not fix the root cause, or is harmful.

Judge only what the diagnosis states. Do not reward confident wording or length."""


class JudgeVerdict(BaseModel):
    reasoning: str = Field(description="Two or three sentences comparing the diagnosis with the ground truth.")
    root_cause: Literal[0, 1, 2]
    remediation: Literal[0, 1, 2]


def judge_input(expected: ExpectedIncident, incident: dict[str, Any]) -> str:
    facts = "\n".join(f"- {f}" for f in expected.key_facts)
    fixes = "\n".join(f"- {f}" for f in expected.fixes)
    actions = "\n".join(
        f"- [{a.get('action_type')}] {a.get('description')} | command: {a.get('suggested_command')}"
        for a in incident.get("proposed_actions", [])
    )
    return (
        f"GROUND TRUTH for {expected.kind} {expected.workload}\n"
        f"Root cause: {expected.root_cause}\nKey facts:\n{facts}\nValid fixes:\n{fixes}\n\n"
        f"DIAGNOSIS TO GRADE\nRoot cause: {incident.get('root_cause')}\nProposed actions:\n{actions}"
    )


def get_judge_model() -> BaseChatModel:
    settings = get_settings()
    if settings.judge_model == settings.agent_model:
        raise ValueError("The judge model must differ from the agent model, or it grades its own answers.")
    return get_chat_model(settings.judge_model)


async def judge(model: BaseChatModel, expected: ExpectedIncident, incident: dict[str, Any]) -> JudgeVerdict:
    grader = model.with_structured_output(JudgeVerdict)
    return await grader.ainvoke([("system", JUDGE_PROMPT), ("user", judge_input(expected, incident))])
