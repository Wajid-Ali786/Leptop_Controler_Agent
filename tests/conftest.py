"""
Shared test fixtures and markers (docs/step3 Section 5).

real_api marker: the test makes real Claude API calls. It is skipped unless
RUN_REAL_CLAUDE_TEST=1 is set, and it records its spend in the real
data/claude_usage.db, so it counts toward the real budget and rate limit.
Select these tests with:  pytest -m real_api

isolated_logging (autouse): relative log paths resolve under each test's own tmp_path, and
so do relative ledger paths - for every test EXCEPT real_api tests. Offline tests therefore
never touch the real logs/ or data/ folders. Logging handlers are removed after each test, and
the Executor forgets the windows it opened, so no test's windows leak into another's close_app.

fake_claude: an offline Claude for tests/test_brain*.py - temp config.yaml, .env and
usage ledger, a controllable clock, and a mock HTTP transport running the real
anthropic SDK. No internet, no real API key, no cost.
"""
import os
from datetime import datetime

import anthropic
import httpx2
import pytest

from app import logging_setup
from app.brain import adapter, cost_controls
from app.executor import logic as executor_logic
from config import settings

REAL_API_OPT_IN = "RUN_REAL_CLAUDE_TEST"
REAL_DESKTOP_OPT_IN = "RUN_REAL_DESKTOP_TEST"
REAL_CLIPBOARD_OPT_IN = "RUN_REAL_CLIPBOARD_TEST"
REAL_ELEVATED_OPT_IN = "RUN_ELEVATED_TEST"
FAKE_KEY = "sk-ant-test-not-a-real-key-12345"
START_TIME = datetime(2026, 9, 15, 12, 0, 0).timestamp()  # local noon, mid-month
OK_BODY = {
    "id": "msg_test", "type": "message", "role": "assistant", "model": "test-model",
    "content": [{"type": "text", "text": "OK"}],
    "stop_reason": "end_turn", "stop_sequence": None,
    "usage": {"input_tokens": 12, "output_tokens": 1},
}


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        f"real_api: makes real Claude API calls (costs a few tokens); skipped unless {REAL_API_OPT_IN}=1; "
        "records spend in the real data/claude_usage.db",
    )
    config.addinivalue_line(
        "markers",
        f"real_desktop: opens real windows on this computer and closes only the ones it opened; "
        f"skipped unless {REAL_DESKTOP_OPT_IN}=1",
    )
    config.addinivalue_line(
        "markers",
        f"real_clipboard: replaces the contents of this computer's clipboard; skipped unless {REAL_CLIPBOARD_OPT_IN}=1 "
        f"(and, being a desktop test, {REAL_DESKTOP_OPT_IN}=1)",
    )
    config.addinivalue_line(
        "markers",
        f"real_elevated: needs a Notepad YOU started with 'Run as administrator' in front, so it can't run "
        f"unattended; skipped unless {REAL_ELEVATED_OPT_IN}=1 (and, being a desktop test, {REAL_DESKTOP_OPT_IN}=1)",
    )


OPT_IN_GATES = {
    "real_api": (REAL_API_OPT_IN,
                 f"Real Claude API call - set {REAL_API_OPT_IN}=1 to run (uses the .env key, costs a few tokens)"),
    "real_desktop": (REAL_DESKTOP_OPT_IN,
                     f"Opens real windows - set {REAL_DESKTOP_OPT_IN}=1 to run (closes only what it opened)"),
    "real_clipboard": (REAL_CLIPBOARD_OPT_IN,
                       f"Replaces your clipboard's contents - set {REAL_CLIPBOARD_OPT_IN}=1 to run"),
    "real_elevated": (REAL_ELEVATED_OPT_IN,
                      f"Needs an elevated Notepad you start and focus by hand - set {REAL_ELEVATED_OPT_IN}=1 to run"),
}


def pytest_collection_modifyitems(config, items):
    """The single gate for opt-in tests: skip them unless explicitly opted in."""
    for marker, (variable, reason) in OPT_IN_GATES.items():
        if os.environ.get(variable) == "1":
            continue
        skip = pytest.mark.skip(reason=reason)
        for item in items:
            if item.get_closest_marker(marker):
                item.add_marker(skip)


@pytest.fixture(autouse=True)
def isolated_logging(request, tmp_path, monkeypatch):
    """Relative log/ledger paths resolve under tmp_path, so offline tests never touch real logs/
    or data/. real_api tests keep the real ledger, so their spend counts toward the real budget."""
    monkeypatch.setattr(logging_setup, "PROJECT_ROOT", tmp_path)
    if request.node.get_closest_marker("real_api") is None:
        monkeypatch.setattr(cost_controls, "PROJECT_ROOT", tmp_path)
    executor_logic.forget_session_windows()
    yield
    executor_logic.forget_session_windows()
    logging_setup.reset_logging()


def config_text(ledger_path, *, model="test-model", rate_limit=5, max_input_tokens=1000,
                max_output_tokens=100, daily_usd=1.0, monthly_usd=5.0) -> str:
    """Test config.yaml. model=None omits brain.model. The cost section comes last."""
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
        "safety:\n"
        '  risky_keywords: [delete, shutdown, "shut down", send]\n'
        "  safe_words: [sender, senders]\n"
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
