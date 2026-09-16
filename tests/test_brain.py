"""
Tests for app/brain/adapter.py (cost controls: tests/test_brain_cost_controls.py).

The offline path uses the fake_claude fixture (tests/conftest.py): the real anthropic
SDK against a mock HTTP transport with a fake key - no internet, no real API key, no
cost. One real-API test is marked real_api (skipped unless RUN_REAL_CLAUDE_TEST=1).
"""
import ast
import json

import httpx2
import pytest

from app.brain import adapter
from app.brain.adapter import ClaudeAuthError, ClaudeRequestError, ClaudeUnavailableError
from app.brain.models import ClaudeReply
from config import settings
from config.settings import MissingSettingError, get_setting
from tests.conftest import FAKE_KEY


def error_response(status: int, error_type: str) -> httpx2.Response:
    return httpx2.Response(status, json={"type": "error", "error": {"type": error_type, "message": "test error"}})


# --- Mocked happy path ---

def test_ping_returns_reply_through_adapter(fake_claude):
    reply = adapter.ping()
    assert reply == ClaudeReply(text="OK", model="test-model", stop_reason="end_turn",
                                input_tokens=12, output_tokens=1)


def test_request_is_built_from_settings(fake_claude):
    adapter.ping()
    request = fake_claude.requests[0]
    body = json.loads(request.content)
    assert request.url.path == "/v1/messages"
    assert body["model"] == "test-model"
    assert body["max_tokens"] == 16
    assert body["messages"] == [{"role": "user", "content": adapter.PING_PROMPT}]
    assert request.headers["x-api-key"] == FAKE_KEY


def test_model_name_comes_from_config(fake_claude):
    fake_claude.configure(model="other-model")
    adapter.ping()
    assert json.loads(fake_claude.requests[0].content)["model"] == "other-model"


# --- Errors: clean, typed, never exposing the key ---

@pytest.mark.parametrize("status, error_type, expected", [
    (401, "authentication_error", ClaudeAuthError),
    (403, "permission_error", ClaudeAuthError),
    (404, "not_found_error", ClaudeRequestError),
    (400, "invalid_request_error", ClaudeRequestError),
    (429, "rate_limit_error", ClaudeUnavailableError),
    (500, "api_error", ClaudeUnavailableError),
    (529, "overloaded_error", ClaudeUnavailableError),
])
def test_api_errors_become_clean_errors(fake_claude, status, error_type, expected):
    fake_claude.respond = lambda request: error_response(status, error_type)
    with pytest.raises(expected) as info:
        adapter.ping()
    assert FAKE_KEY not in str(info.value)
    assert info.value.__cause__ is None  # SDK exception (with request headers) not chained


def test_network_unavailable_fails_gracefully(fake_claude):
    def no_network(request):
        raise httpx2.ConnectError("network unreachable", request=request)
    fake_claude.respond = no_network
    with pytest.raises(ClaudeUnavailableError, match="Can't reach Claude"):
        adapter.ping()


def test_timeout_fails_gracefully(fake_claude):
    def too_slow(request):
        raise httpx2.ReadTimeout("timed out", request=request)
    fake_claude.respond = too_slow
    with pytest.raises(ClaudeUnavailableError, match="did not respond in time"):
        adapter.ping()


@pytest.mark.parametrize("respond", [
    lambda request: httpx2.Response(200, json={"unexpected": True}),
    lambda request: httpx2.Response(200, text="not json at all"),
])
def test_malformed_response_is_caught(fake_claude, respond):
    fake_claude.respond = respond
    with pytest.raises(ClaudeRequestError):
        adapter.ping()


def test_missing_api_key_fails_before_any_request(fake_claude):
    fake_claude.env_path.write_text("ANTHROPIC_API_KEY=\n", encoding="utf-8")
    with pytest.raises(MissingSettingError):
        adapter.ping()
    assert fake_claude.requests == []


def test_missing_model_setting_fails_before_any_request(fake_claude):
    fake_claude.configure(model=None)
    with pytest.raises(MissingSettingError, match="brain.model"):
        adapter.ping()
    assert fake_claude.requests == []


# --- Config + architecture rules ---

def test_real_config_defines_brain_settings():
    assert isinstance(get_setting("brain.model"), str) and get_setting("brain.model")
    assert get_setting("brain.timeout_seconds") > 0
    assert get_setting("brain.max_retries") >= 0
    assert get_setting("brain.ping_max_tokens") > 0


def test_only_brain_adapter_imports_anthropic():
    root = settings.PROJECT_ROOT
    allowed = root / "app" / "brain" / "adapter.py"
    files = [*(root / "app").rglob("*.py"), *(root / "config").rglob("*.py"),
             *(root / "scripts").rglob("*.py"), root / "main.py"]
    offenders = []
    for path in files:
        if path == allowed:
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            offenders += [f"{path.relative_to(root)}: {n}" for n in names
                          if n.split(".")[0] in ("anthropic", "httpx", "httpx2")]
    assert offenders == [], f"Only app/brain/adapter.py may import the Claude SDK: {offenders}"


# --- Optional real API call (skipped by default) ---

@pytest.mark.real_api
def test_real_claude_ping():
    reply = adapter.ping()
    assert reply.input_tokens > 0
    assert reply.output_tokens > 0
