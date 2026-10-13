"""Chat model factory, so the provider is a setting rather than a code change."""

from langchain_core.language_models import BaseChatModel

from kube_sre.config import get_settings


def get_chat_model(model: str | None = None) -> BaseChatModel:
    """Return a chat model for the configured provider. `model` defaults to the agent model."""
    settings = get_settings()
    model = model or settings.agent_model
    if settings.llm_provider == "openai":
        from langchain_openai import ChatOpenAI

        return ChatOpenAI(model=model)
    from langchain_aws import ChatBedrockConverse

    return ChatBedrockConverse(model=model, region_name=settings.aws_region)
