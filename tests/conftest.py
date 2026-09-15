"""
Shared test fixtures (docs/step3 Section 5).

isolated_logging (autouse): every test's log output goes to its own tmp_path/logs/,
never the real logs/ folder, and logging handlers are removed after each test.

fake_claude: an offline Claude for tests/test_brain*.py - temp config.yaml, .env and
usage ledger, a controllable clock, and a mock HTTP transport running the real
anthropic SDK. No internet, no real API key, no cost.
"""
from datetime import datetime

import anthropic
import httpx2
import pytest

from app import logging_setup
from app.brain import adapter, cost_controls
from config import settings

FAKE_KEY = "sk-ant-test-not-a-real-key-12345"
START_TIME = datetime(2026, 9, 15, 12, 0, 0).timestamp()  # local noon, mid-month
OK_BODY = {
    "id": "msg_test", "type": "message", "role": "assistant", "model": "test-model",
    "content": [{"type": "text", "text": "OK"}],
    "stop_reason": "end_turn", "stop_sequence": None,
    "usage": {"input_tokens": 12, "output_tokens": 1},
}


@pytest.fixture(autouse=True)
def isolated_logging(tmp_path, monkeypatch):
    """Relative log paths resolve under tmp_path, so tests never write to the real logs/."""
    monkeypatch.setattr(logging_setup, "PROJECT_ROOT", tmp_path)
    yield
    logging_setup.reset_logging()


def config_text(ledger_path, *, model="test-model", rate_limit=5, max_input_tokens=1000,
                max_output_tokens=100, daily_usd=1.0, monthly_usd=5.0) -> str:
    """Test config.yaml. model=None omits brain.model."""
    model_line = f"  model: {model}\n" if model else ""
    return (
        "logging:\n"
        "  file: logs/companion.log\n"
        "  level: INFO\n"
        "  max_bytes: 100000\n"
        "  backup_count: 2\n"
        "brain:\n"
        f"{model_line}"
        "  timeout_seconds: 5\n"
        "  max_retries: 0\n"
        "  ping_max_tokens: 16\n"
        "cost:\n"
        f"  rate_limit_per_minute: {rate_limit}\n"
        f"  max_input_tokens_per_request: {max_input_tokens}\n"
        f"  max_output_tokens_per_request: {max_output_tokens}\n"
        f"  daily_budget_usd: {daily_usd}\n"
        f"  monthly_budget_usd: {monthly_usd}\n"
        f'  usage_db_path: "{ledger_path.as_posix()}"\n'
        "  prices_usd_per_million_tokens:\n"
        "    test-model: {input: 5.0, output: 25.0}\n"
        "    other-model: {input: 1.0, output: 5.0}\n"
    )


class FakeClaude:
    """Stands in for the Claude API: records requests, returns whatever `respond` builds."""

    def __init__(self, tmp_path):
        self.requests = []
        self.respond = lambda request: httpx2.Response(200, json=OK_BODY)
        self.now = START_TIME
        self.config_path = tmp_path / "config.yaml"
        self.env_path = tmp_path / ".env"
        self.ledger_path = tmp_path / "claude_usage.db"
        self.log_path = tmp_path / "logs" / "companion.log"

    def handle(self, request):
        self.requests.append(request)
        return self.respond(request)

    def configure(self, **overrides):
        self.config_path.write_text(config_text(self.ledger_path, **overrides), encoding="utf-8")

    def reply_with_usage(self, input_tokens: int, output_tokens: int):
        body = {**OK_BODY, "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens}}
        self.respond = lambda request: httpx2.Response(200, json=body)


@pytest.fixture
def fake_claude(tmp_path, monkeypatch):
    fake = FakeClaude(tmp_path)
    fake.configure()
    fake.env_path.write_text(f"ANTHROPIC_API_KEY={FAKE_KEY}\n", encoding="utf-8")
    monkeypatch.setattr(settings, "CONFIG_PATH", fake.config_path)
    monkeypatch.setattr(settings, "ENV_PATH", fake.env_path)
    for var in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(cost_controls, "_now", lambda: fake.now)

    real_get_client = adapter.get_client
    monkeypatch.setattr(adapter, "get_client", lambda: real_get_client(
        http_client=anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(fake.handle))
    ))
    return fake
