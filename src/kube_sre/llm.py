"""Chat model factory, so the agent and the eval judge build their models the same way."""

from langchain_core.language_models import BaseChatModel
from langchain_openai import ChatOpenAI

from kube_sre.config import get_settings


def get_chat_model(model: str | None = None) -> BaseChatModel:
    """Return an OpenAI chat model. `model` defaults to the agent model."""
    settings = get_settings()
    return ChatOpenAI(
        model=model or settings.agent_model,
        timeout=settings.llm_request_timeout_seconds,
        max_retries=settings.llm_max_retries,
    )
