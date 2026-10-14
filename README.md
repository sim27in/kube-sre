# kube-sre

[![ci](https://github.com/sim27in/kube-sre/actions/workflows/ci.yml/badge.svg)](https://github.com/sim27in/kube-sre/actions/workflows/ci.yml)

An LLM agent that investigates failing Kubernetes workloads with read-only tools and returns a typed incident report. Every piece of evidence in the report cites the id of the tool result it came from, and the agent checks those citations before printing the report.

It's a learning project that runs entirely on your machine against a local [kind](https://kind.sigs.k8s.io/) cluster. The only external service is the LLM API.

> **Status:** early. Phases 1 and 2 are done: typed tools, redaction, evidence ids, a citation check, and an offline eval harness. On 13 recorded failure scenarios the agent gets the root cause fully right in 97% of runs, up from 54% for the original prototype.

## What it does

Point it at a namespace and it finds the unhealthy workloads, gathers evidence (pod status, logs, events, the workload spec), and writes a report. Excerpt from a real run against the two broken demo apps in [`k8s/`](k8s):

```json
{
  "resource_kind": "Deployment",
  "resource_name": "analytics-processor",
  "category": "oom_killed",
  "root_cause": "The container runs a Python command that allocates ~100MB while the Pod sets a memory limit of 32Mi; the process is OOMKilled on startup and enters a CrashLoopBackOff.",
  "evidence": [
    { "evidence_id": "ev-b04ecfd508", "source": "pod_status", "detail": "... last_termination.reason=OOMKilled last_termination.exit_code=137" },
    { "evidence_id": "ev-2d4630d821", "source": "spec", "detail": "... limits.memory=32Mi" }
  ],
  "proposed_actions": [
    { "action_type": "patch_resources", "suggested_command": "kubectl set resources deployment analytics-processor --containers=memory-hog --limits=memory=256Mi --requests=memory=128Mi -n default" }
  ],
  "confidence": 0.9
}
Citations: all 7 match tool results.
Tokens: 6319 input, 3087 output
```

One investigation takes about 30 seconds and costs about $0.01 on `gpt-5-mini`.

## How it works

```
agent.py ──MCP stdio──▶ tool server (tools/k8s_read.py) ──▶ Kubernetes API (read-only)
   ▲                          │
   │                          └─ boundary.finalize(): redact secrets + PII, stamp evidence id
   └── TriageReport (Pydantic): category, root cause, cited evidence, proposed kubectl commands
```

- **Typed tools.** `list_pods`, `get_pod_logs`, `get_events`, `list_warning_events`, `list_pvcs`, `get_owner` and `describe_workload` return Pydantic models with capped sizes, not raw API output. Exit codes, termination reasons and why a pod is Pending come pre-extracted.
- **Redaction at the boundary.** Every string leaving the tool server is scrubbed before the model or a trace sees it. Secrets become `<redacted:jwt>`-style markers. Emails, public IPs and person names become stable pseudonyms like `<email:3fa2c1>`, so the agent can still tell when the same value appears twice. Private cluster IPs are kept because they help diagnosis.
- **Evidence ids.** Each tool result gets a deterministic `ev-…` id (a hash of the call and its redacted output). The report schema requires evidence to cite one, and the agent flags any cited id that no tool returned.
- **Read-only.** The tools never write to the cluster and never read Secret values; `describe_workload` shows which Secret keys a container references, not their contents.
- **Frozen baseline.** The original prototype is kept in [`baselines/v0`](baselines/v0) so later changes can be measured against it.

## Evals

13 failure scenarios live in [`evals/scenarios`](evals/scenarios), each a manifest plus the known answer (workload, accepted categories, root cause, key facts, valid fixes). Each was applied to kind once and **snapshotted at the API level** ([`evals/snapshots`](evals/snapshots)): pods, events, PVCs, workloads and every container's logs. At eval time a fake Kubernetes client serves the snapshot to the *unchanged* tool server, so any tool call with any arguments works offline, for both the v1 tools and the frozen v0 ones. Evals need an OpenAI key but no cluster.

Each run is scored in code (incident found, workload named, category, false positives, invalid citations, whether it read the spec when the answer is only there) and by an LLM judge (root cause and remediation, 0-2 each, against the key facts). A run passes only if everything is right.

gpt-5-mini, 13 scenarios × 3 runs each:

| | v0 prototype | v1, Phase 1 prompt | **v1, current** |
|---|---|---|---|
| Runs passed | 44% | 64% | **79%** |
| Incidents detected | 92% | 97% | **100%** |
| Root cause fully correct | 54% | 87% | **97%** |
| Remediation fixes the cause | 74% | 79% | **85%** |
| False positives | 2 | 1 | **0** |
| Invalid citations | n/a | 0 / 169 | 0 / 179 |
| Tokens per run (in / out) | 4.5k / 2.0k | 7.0k / 2.3k | 9.7k / 2.8k |

v0 fails exactly where its tools are blind: it can't read a workload spec (so it never sees a 32Mi limit or a 64-CPU request), can't pick an init container's logs, and has no namespace-wide events, so on a Deployment whose pods are all rejected by a ResourceQuota it lists pods 20 times until the step limit. The jump from 64% to 79% is four system prompt rules, each added because a failing scenario pointed at it.

```sh
uv run python -m kube_sre.evals run                       # v0 and v1, all scenarios, 3 repeats (78 agent runs)
uv run python -m kube_sre.evals run --agents v1 --scenarios oom-killed mixed --repeats 1
uv run python -m kube_sre.evals rescore evals/results/<run>   # re-judge after editing a scenario
uv run python -m kube_sre.evals record oom-killed        # re-record a snapshot (needs kind)
```

Results go to `evals/results/<timestamp>/` as `runs.jsonl` (every report, tool call and judge verdict) and `summary.md`.

## Setup

### Prerequisites

- [Docker](https://docs.docker.com/get-docker/) (Docker Desktop on macOS; give it at least 4 GB of memory)
- [kind](https://kind.sigs.k8s.io/docs/user/quick-start/#installation) and [kubectl](https://kubernetes.io/docs/tasks/tools/)
- [uv](https://docs.astral.sh/uv/getting-started/installation/). It installs Python 3.12 for you if needed.
- An [OpenAI API key](https://platform.openai.com/api-keys). Set a monthly usage limit in the dashboard.

### 1. Clone and install

```sh
git clone https://github.com/sim27in/kube-sre.git
cd kube-sre
uv sync
```

### 2. Create a local cluster with two broken apps

```sh
kind create cluster --name kubesre
kubectl apply -f k8s/
kubectl get pods   # after a minute: one ImagePullBackOff, one CrashLoopBackOff (OOMKilled)
```

### 3. Configure

```sh
cp .env.example .env
```

Put your key in `.env` as `OPENAI_API_KEY=...`. `.env` is gitignored. LangSmith tracing is optional: add `LANGSMITH_API_KEY` to see each tool call and model step, or set `LANGSMITH_TRACING=false`.

Variables exported in your shell take precedence over `.env`. If you get a 401 from OpenAI, look for a stale `OPENAI_API_KEY` in your shell profile.

### 4. Run

```sh
uv run python -m kube_sre.agent                    # inspects the default namespace
uv run python -m kube_sre.agent --namespace demo   # or another namespace
```

### 5. Clean up

```sh
kind delete cluster --name kubesre
```

## Configuration

Settings are read from `KUBE_SRE_*` environment variables or `.env`. See [`config.py`](src/kube_sre/config.py) for the full list.

| Variable | Default | Meaning |
|---|---|---|
| `KUBE_SRE_AGENT_MODEL` | `gpt-5-mini` | Model the agent uses |
| `KUBE_SRE_KUBE_CONTEXT` | current context | kubeconfig context to use |
| `KUBE_SRE_AGENT_RECURSION_LIMIT` | `40` | Maximum agent steps before giving up |
| `KUBE_SRE_AGENT_TIMEOUT_SECONDS` | `300` | Wall-clock limit for one investigation |
| `KUBE_SRE_MAX_LOG_BYTES` | `16000` | Log output cap per call; the most recent lines are kept |
| `KUBE_SRE_REDACT_NAMES` | `true` | Person-name detection with Presidio (regex rules always run) |
| `KUBE_SRE_JUDGE_MODEL` | `gpt-6.1-sol` | Eval judge; must differ from the agent model |

## Development

```sh
uv run pytest            # unit tests, including snapshot replay; no cluster or API key needed
uv run pytest -m kind    # integration tests against the kind cluster
uv run ruff check . && uv run ruff format --check src tests
```

The tool logic lives in plain `*_impl(client, ...)` functions that the MCP tools wrap, so unit tests run them against fake Kubernetes clients.

```
src/kube_sre/
  agent.py              entry point: spawns the tool server, runs the agent, checks citations
  schemas.py            the report schema
  citations.py          checks cited evidence ids against tool results
  config.py, llm.py     settings and chat model factory
  tools/k8s_read.py     the read-only tools, served over MCP
  tools/models.py       typed tool outputs
  tools/boundary.py     redaction + evidence id for every tool result
  guardrails/redaction.py
  evals/                scenarios, snapshot record/replay, scoring, LLM judge, runner (python -m kube_sre.evals)
baselines/v0/           the original prototype, frozen
evals/scenarios/        13 broken setups with their known answers
evals/snapshots/        recorded API state per scenario
k8s/                    broken demo apps
tests/                  unit tests; tests/integration needs kind
```

## Roadmap

Everything stays local: kind on a laptop, no cloud deployment. Each phase ends with a measured result.

| Phase | Scope | Status |
|---|---|---|
| 0 | Repo setup, frozen v0 baseline | ✅ |
| 1 | Typed tools, redaction, evidence ids, citation check | ✅ |
| 2 | Eval harness: 13 failure scenarios, API snapshots so evals run offline, an LLM judge for root causes, v0 vs v1 | ✅ |
| 3 | Alert-driven: Alertmanager webhook, Prometheus and Loki tools, log compression, `make up` for the whole local stack | next |
| 4 | LangGraph multi-agent, scored against the single agent on the same tools and model | stretch |
| 5 | Runbook RAG with local embeddings, incident memory | stretch |
| 6 | Remediation behind human approval and a deny-by-default policy | stretch |
| 7 | Local models via Ollama (no API key needed), cost routing | optional |

## Lessons so far

- **A redaction regex missed `DB_PASSWORD=`.** A `\b` boundary never matches after `_`. A test with seeded secrets caught it.
- **The kubelet reports missing logs as log text.** For a crash-looping container, `previous=True` can return "unable to retrieve container logs…" with HTTP 200. The tool detects it and falls back to the right instance.
- **Name detection mistook `grafana` and `kafka` for people.** The agent would have received a pseudonym instead of the workload name. Name detection now runs only on free-text fields.
- **MCP tools from `get_tools()` start a new server process per call.** One persistent session made runs much faster.
- **The same model takes different paths on the same cluster.** One run checked the workload spec and found the 32Mi limit; another skipped it and only named the symptom. Measured: the agent read the spec in 47% of the runs that needed it.
- **A prompt fix that works on its target can still be a regression.** "Read the spec before blaming limits or probes" took spec reads to 100%, and the agent then read a healthy Deployment's spec and reported old probe warnings as an incident (3 of 3 runs). Only the full scenario set showed it.
- **Check the grader before the agent.** The first baseline failed correct diagnoses for not quoting an exit code. Key facts must state the diagnosis, not the evidence, and reading the judge's reasoning for every failure was the only way to catch it.
- **Record the API, not the tool calls.** Tool-call cassettes break the moment the agent picks a different argument. A snapshot of the API state replays any call, for both agent versions.

## License

[Apache License 2.0](LICENSE). Copyright 2026 sim27in.
