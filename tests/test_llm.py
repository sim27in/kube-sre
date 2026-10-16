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


def test_requests_time_out_and_retry(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    model = get_chat_model()
    assert model.request_timeout == get_settings().llm_request_timeout_seconds
    assert model.max_retries == get_settings().llm_max_retries
