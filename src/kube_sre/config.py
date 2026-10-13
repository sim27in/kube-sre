import os
from functools import lru_cache
from typing import Literal

from kubernetes import client, config
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="KUBE_SRE_", env_file=".env", extra="ignore")

    # Kubernetes
    kube_context: str | None = None  # None = current context (ignored in-cluster)

    # Tool output bounds, so one noisy pod can't blow the context window
    max_log_bytes: int = 16_000
    max_log_lines: int = 500
    max_events: int = 30
    max_pods: int = 100

    # Redaction
    redact_names: bool = True  # Presidio PERSON detection; regex rules always run
    pseudonym_salt: str = "kube-sre"  # stable placeholders across calls and replays

    # LLM. Credentials come from the provider's own env vars (OPENAI_API_KEY, or AWS credentials).
    llm_provider: Literal["openai", "bedrock"] = "openai"
    agent_model: str = "gpt-5-mini"
    judge_model: str = "gpt-6.1-sol"  # used by evals (Phase 2); must differ from the model under test
    aws_region: str = "us-east-1"  # bedrock only


@lru_cache
def get_settings() -> Settings:
    return Settings()


@lru_cache
def _api_client() -> client.ApiClient:
    """Load kube config on first use. In-cluster when running in a pod, kubeconfig otherwise."""
    if os.getenv("KUBERNETES_SERVICE_HOST"):
        config.load_incluster_config()
    else:
        config.load_kube_config(context=get_settings().kube_context)
    return client.ApiClient()


def get_core_api() -> client.CoreV1Api:
    return client.CoreV1Api(_api_client())


def get_apps_api() -> client.AppsV1Api:
    return client.AppsV1Api(_api_client())
