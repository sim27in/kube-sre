from langchain_openai import ChatOpenAI

from kube_sre.config import get_settings
from kube_sre.llm import get_chat_model


def test_agent_model_is_default(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    model = get_chat_model()
    assert isinstance(model, ChatOpenAI)
    assert model.model_name == get_settings().agent_model


def test_model_override(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    assert get_chat_model("gpt-5").model_name == "gpt-5"
