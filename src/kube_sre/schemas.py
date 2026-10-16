from enum import StrEnum

from pydantic import BaseModel, Field


class Severity(StrEnum):
    CRITICAL = "critical"  # user-facing outage, no healthy replicas
    HIGH = "high"  # service degraded or crash-looping
    MEDIUM = "medium"  # partial impact, some replicas healthy
    LOW = "low"  # no current impact, warning signs only


class FailureCategory(StrEnum):
    IMAGE_PULL = "image_pull"  # ErrImagePull / ImagePullBackOff
    OOM_KILLED = "oom_killed"  # container exceeded memory limit
    CRASH_LOOP = "crash_loop"  # app exits on startup (non-OOM)
    CONFIG_ERROR = "config_error"  # missing/invalid ConfigMap, Secret, env
    SCHEDULING = "scheduling"  # Pending: insufficient resources, taints, affinity
    PROBE_FAILURE = "probe_failure"  # liveness/readiness probe failing
    OTHER = "other"


class EvidenceSource(StrEnum):
    POD_STATUS = "pod_status"
    LOGS = "logs"
    EVENTS = "events"
    METRICS = "metrics"
    SPEC = "spec"
    OWNERSHIP = "ownership"  # get_owner
    STORAGE = "storage"  # PVC status
    NODES = "nodes"  # list_nodes
    SERVICE = "service"  # describe_service
    CONFIG = "config"  # describe_config


class ActionType(StrEnum):
    ROLLOUT_RESTART = "rollout_restart"
    SET_IMAGE = "set_image"
    PATCH_RESOURCES = "patch_resources"
    SCALE = "scale"
    MANUAL = "manual"  # needs a human/code change; not executable by the agent


class Evidence(BaseModel):
    evidence_id: str = Field(description="The evidence_id of the tool result this observation comes from.")
    source: EvidenceSource = Field(description="Which tool/signal this evidence came from.")
    detail: str = Field(description="The specific observation, quoted from tool output where possible.")


class RemediationAction(BaseModel):
    action_type: ActionType
    description: str = Field(description="What the action does and why it fixes the root cause.")
    suggested_command: str = Field(
        description="Equivalent kubectl command for a human operator to review. It must work as written."
    )


class IncidentReport(BaseModel):
    """Diagnosis of a single failing workload."""

    resource_kind: str = Field(description="Kubernetes kind of the owning workload, e.g. Deployment.")
    resource_name: str = Field(description="Name of the owning workload, e.g. payment-service.")
    pod_name: str = Field(description="Name of the failing pod that was investigated.")
    namespace: str
    category: FailureCategory = Field(
        description=(
            "image_pull: the image can't be pulled (bad tag, missing pull secret). "
            "oom_killed: a container is killed for exceeding its memory limit. "
            "crash_loop: the app exits on its own (error, bad command, failing init container), not OOM. "
            "config_error: a missing or invalid ConfigMap, Secret, env value, StorageClass or quota-violating spec. "
            "scheduling: a pod exists but stays Pending because no node fits (resources, taints, affinity). "
            "If no pod was created at all (ResourceQuota, admission), it is not scheduling. "
            "probe_failure: a liveness or readiness probe fails. "
            "other: none of these."
        )
    )
    severity: Severity
    root_cause: str = Field(description="One or two sentences stating the underlying cause, not the symptom.")
    evidence: list[Evidence] = Field(min_length=1, description="Observations supporting the root cause.")
    proposed_actions: list[RemediationAction] = Field(min_length=1)
    confidence: float = Field(ge=0.0, le=1.0, description="Confidence in the root cause, 0 to 1.")


class TriageReport(BaseModel):
    """Result of investigating a namespace; one IncidentReport per failing workload."""

    namespace: str
    summary: str = Field(description="Short overall summary of namespace health.")
    incidents: list[IncidentReport] = Field(description="Empty if all pods are healthy.")
