# kube-sre

[![ci](https://github.com/sim27in/kube-sre/actions/workflows/ci.yml/badge.svg)](https://github.com/sim27in/kube-sre/actions/workflows/ci.yml)

An LLM agent that investigates failing Kubernetes workloads with read-only tools and returns a typed incident report. Every piece of evidence in the report cites the id of the tool result it came from, and the agent checks those citations before printing the report.

It's a learning project that runs entirely on your machine against a local [kind](https://kind.sigs.k8s.io/) cluster. The only external service is the LLM API.

> **Status:** early. Phase 1 of the roadmap is done: typed tools, redaction, evidence ids and a citation check. An eval harness comes next.

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

## Development

```sh
uv run pytest            # unit tests, no cluster or API key needed
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
baselines/v0/           the original prototype, frozen
k8s/                    broken demo apps
tests/                  unit tests; tests/integration needs kind
```

## Roadmap

Everything stays local: kind on a laptop, no cloud deployment. Each phase ends with a measured result.

| Phase | Scope | Status |
|---|---|---|
| 0 | Repo setup, frozen v0 baseline | ✅ |
| 1 | Typed tools, redaction, evidence ids, citation check | ✅ |
| 2 | Eval harness: ~12 failure scenarios, recorded tool calls so evals run offline, an LLM judge for root causes, v0 vs v1 | next |
| 3 | Alert-driven: Alertmanager webhook, Prometheus and Loki tools, log compression, `make up` for the whole local stack | planned |
| 4 | LangGraph multi-agent, scored against the single agent on the same tools and model | stretch |
| 5 | Runbook RAG with local embeddings, incident memory | stretch |
| 6 | Remediation behind human approval and a deny-by-default policy | stretch |
| 7 | Local models via Ollama (no API key needed), cost routing | optional |

## Lessons so far

- **A redaction regex missed `DB_PASSWORD=`.** A `\b` boundary never matches after `_`. A test with seeded secrets caught it.
- **The kubelet reports missing logs as log text.** For a crash-looping container, `previous=True` can return "unable to retrieve container logs…" with HTTP 200. The tool detects it and falls back to the right instance.
- **Name detection mistook `grafana` and `kafka` for people.** The agent would have received a pseudonym instead of the workload name. Name detection now runs only on free-text fields.
- **MCP tools from `get_tools()` start a new server process per call.** One persistent session made runs much faster.
- **The same model takes different paths on the same cluster.** One run checked the workload spec and found the 32Mi limit; another skipped it and only named the symptom. That's why the evals will run each scenario several times.

## License

[Apache License 2.0](LICENSE). Copyright 2026 sim27in.
