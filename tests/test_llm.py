from langchain_aws import ChatBedrockConverse
from langchain_openai import ChatOpenAI

from kube_sre.config import get_settings
from kube_sre.llm import get_chat_model


def test_openai_is_default(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    model = get_chat_model()
    assert isinstance(model, ChatOpenAI)
    assert model.model_name == get_settings().agent_model


def test_bedrock_selected_by_setting(monkeypatch):
    monkeypatch.setattr(get_settings(), "llm_provider", "bedrock")
    model = get_chat_model("amazon.nova-lite-v1:0")
    assert isinstance(model, ChatBedrockConverse)
    assert model.model_id == "amazon.nova-lite-v1:0"
