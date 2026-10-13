"""Typed outputs for the read-only Kubernetes tools.

Every result carries an `evidence_id`, minted at the tool boundary, which reports must cite.
"""

from typing import Literal

from pydantic import BaseModel, Field

WorkloadKind = Literal["Deployment", "StatefulSet", "DaemonSet"]


class ToolResult(BaseModel):
    evidence_id: str = Field(default="", description="Cite this id in the report for claims based on this result.")


class ObjectRef(BaseModel):
    kind: str
    name: str


# --- pods -------------------------------------------------------------------


class TerminationInfo(BaseModel):
    reason: str | None = None  # e.g. OOMKilled, Error, Completed
    exit_code: int | None = None  # 137 = SIGKILL (usually OOM), 1 = app error
    message: str | None = None
    finished_at: str | None = None


class ContainerStatus(BaseModel):
    name: str
    image: str
    ready: bool
    restart_count: int
    state: Literal["running", "waiting", "terminated", "unknown"]
    reason: str | None = None  # waiting/terminated reason, e.g. CrashLoopBackOff
    message: str | None = None
    exit_code: int | None = None  # only when state is terminated
    last_termination: TerminationInfo | None = Field(
        default=None, description="Why the previous instance died. Use get_pod_logs(previous=True) for its logs."
    )


class PodSummary(BaseModel):
    name: str
    namespace: str
    phase: str
    node: str | None = None
    owner: ObjectRef | None = Field(default=None, description="Direct controller, e.g. a ReplicaSet.")
    failing_conditions: list[str] = Field(
        default_factory=list, description="Pod conditions that are not True, e.g. 'PodScheduled: Unschedulable - ...'."
    )
    init_containers: list[ContainerStatus] = Field(default_factory=list)
    containers: list[ContainerStatus] = Field(default_factory=list)


class PodList(ToolResult):
    namespace: str
    total: int
    truncated: bool = False
    pods: list[PodSummary]


class PodLogs(ToolResult):
    pod: str
    namespace: str
    container: str
    previous: bool
    lines: int
    truncated: bool = Field(description="True if older lines were dropped to fit the size cap.")
    note: str | None = Field(default=None, description="Which instance these logs came from, or why they are empty.")
    text: str


# --- events -----------------------------------------------------------------


class EventInfo(BaseModel):
    type: str | None = None  # Normal / Warning
    reason: str | None = None
    message: str | None = None
    count: int = 1
    last_seen: str | None = None
    component: str | None = None


class EventList(ToolResult):
    namespace: str
    object: ObjectRef
    truncated: bool = False
    events: list[EventInfo] = Field(description="Most recent first.")


# --- ownership and specs ----------------------------------------------------


class OwnerChain(ToolResult):
    pod: str
    namespace: str
    chain: list[ObjectRef] = Field(description="Controllers from the pod upwards, e.g. [ReplicaSet, Deployment].")
    workload: ObjectRef = Field(description="Top-level owner; the Pod itself if it has no controller.")


class ProbeSpec(BaseModel):
    handler: Literal["http", "tcp", "exec", "grpc", "unknown"]
    target: str | None = None  # "/healthz:8080", "5432", or the exec command
    initial_delay_seconds: int | None = None
    period_seconds: int | None = None
    timeout_seconds: int | None = None
    failure_threshold: int | None = None


class EnvRef(BaseModel):
    var: str
    source: Literal["value", "secret", "configmap", "field", "resource", "secret_all", "configmap_all"]
    source_name: str | None = None  # Secret/ConfigMap name
    key: str | None = None
    optional: bool | None = None
    value: str | None = Field(default=None, description="Literal value only; secret values are never read.")


class ContainerSpec(BaseModel):
    name: str
    image: str
    command: list[str] | None = None
    args: list[str] | None = None
    requests: dict[str, str] = Field(default_factory=dict)
    limits: dict[str, str] = Field(default_factory=dict)
    liveness: ProbeSpec | None = None
    readiness: ProbeSpec | None = None
    startup: ProbeSpec | None = None
    env: list[EnvRef] = Field(default_factory=list)


class WorkloadSpec(ToolResult):
    kind: WorkloadKind
    name: str
    namespace: str
    revision: str | None = None
    desired: int | None = None
    ready: int | None = None
    available: int | None = None
    updated: int | None = None
    conditions: list[str] = Field(default_factory=list)
    containers: list[ContainerSpec]
