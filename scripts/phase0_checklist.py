"""
Phase 0 pass/fail checklist (docs/step4 Sections 3 and 15) - explicit and repeatable.

    python scripts/phase0_checklist.py                  # offline checks only (default)
    python scripts/phase0_checklist.py --fast           # skip the targeted re-runs of subsets
    python scripts/phase0_checklist.py --with-real-api  # ALSO the real-key checks (costs a few tokens)

Prints PASS/FAIL for every automated check, then PENDING for the items only a human can
close (a real API key, Wi-Fi switched off, confirming prices/limits, committing). Exits 0
only when every automated check passed. Makes no Claude API calls unless --with-real-api.
"""
import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable
BLACKHOLE = "http://127.0.0.1:9"  # nothing listens here, so any real outbound HTTP fails fast

CLAUDE_TESTS = ["tests/test_brain.py", "tests/test_brain_cost_controls.py",
                "tests/test_brain_cost_reservation.py", "tests/test_health.py",
                "tests/test_health_offline_checks.py"]

TARGETED = [
    ("Oversized request blocked by the token/budget limits, not sent",
     ["tests/test_brain_cost_controls.py", "tests/test_brain_cost_reservation.py",
      "-k", "token_limit or budget or worst_case"]),
    ("Health check names exactly what is missing", ["tests/test_health_offline_checks.py"]),
    ("Minimal safety gate enforced (Medium+ needs confirmation)", ["tests/test_safety.py"]),
    ("Emergency stop exists and is callable", ["tests/test_executor.py"]),
    ("Crash/restart doesn't corrupt config, logs or ledger", ["tests/test_recovery.py"]),
]

PENDING = [
    ("Health check with a VALID real key reports all systems OK",
     "create .env with your key, then: python main.py --check-claude"),
    ("Health check with an INVALID key reports a clear, specific failure",
     "put a deliberately wrong key in .env, then: python main.py --check-claude"),
    ("Opt-in real API tests pass",
     "set RUN_REAL_CLAUDE_TEST=1, then: pytest -k real"),
    ("Mocked/local tests pass with NO internet at all",
     "turn Wi-Fi off, then: pytest -q"),
    ("A fresh clone installs and passes",
     "python scripts/fresh_clone_check.py"),
    ("Claude prices confirmed against claude.com/pricing",
     "then keep the prices STATUS line in config/config.yaml up to date"),
    ("Rate/token/budget values confirmed as the ones you want",
     "then set the limits STATUS to verified in config/config.yaml"),
    ("Phase 0 committed and tagged",
     "git add -A, git commit, git tag v0.1"),
]

results = []


def record(name, ok, detail):
    results.append((name, ok))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}\n        {detail}", flush=True)


def pytest(args, env=None, timeout=1800):
    cmd = [PY, "-m", "pytest", "-q", "-p", "no:cacheprovider", *args]
    proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=timeout, env=env)
    lines = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
    summary = next((line for line in reversed(lines)
                    if re.search(r"(passed|failed|error|no tests ran)", line)), "no pytest summary")
    return proc.returncode, summary


def check_pinned_dependencies():
    req_in, req_txt = ROOT / "requirements.in", ROOT / "requirements.txt"
    if not req_in.exists() or not req_txt.exists():
        record("Dependencies pinned", False, "requirements.in and/or requirements.txt is missing")
        return

    def entries(path):
        return [line.strip() for line in path.read_text(encoding="utf-8").splitlines()
                if line.strip() and not line.strip().startswith("#")]

    pins = entries(req_txt)
    chosen = [re.split(r"[#\s]", line)[0].lower() for line in entries(req_in)]
    pinned_names = {p.split("==")[0].lower().replace("_", "-") for p in pins}
    unpinned = [p for p in pins if "==" not in p]
    missing = [c for c in chosen if c.replace("_", "-") not in pinned_names]
    ok = bool(pins) and not unpinned and not missing and "httpx2" in chosen
    record("Dependencies pinned", ok,
           f"{len(pins)} pinned packages, {len(chosen)} chosen in requirements.in; "
           f"unpinned={unpinned or 'none'}; chosen-but-not-installed={missing or 'none'}; "
           f"httpx2 listed={'httpx2' in chosen}")


def check_test_suite():
    code, summary = pytest([])
    record("Test suite passes (offline, no API calls)", code == 0, summary)


def check_network_isolated_tests():
    env = dict(os.environ, HTTP_PROXY=BLACKHOLE, HTTPS_PROXY=BLACKHOLE, ALL_PROXY=BLACKHOLE, NO_PROXY="")
    code, summary = pytest(CLAUDE_TESTS, env=env)
    record("Mocked Claude path works with outbound HTTP blackholed", code == 0,
           f"{summary} (a real Wi-Fi-off run is still PENDING below)")


def check_startup():
    proc = subprocess.run([PY, "main.py"], cwd=ROOT, capture_output=True, text=True, timeout=600)
    out, err = proc.stdout, proc.stderr
    expected = ["API key", "Cost limits", "Model price", "Usage ledger", "Safety rules", "Logging"]
    missing = [name for name in expected if f"] {name}:" not in out]
    has_env = (ROOT / ".env").exists()
    ok = not missing and "Traceback" not in out + err and proc.returncode in (0, 1)
    if not has_env:  # no key configured yet: it must fail on exactly that, cleanly
        ok = ok and proc.returncode == 1 and "[FAIL] API key" in out
    record("main.py startup health check runs cleanly", ok,
           f"exit code {proc.returncode}; .env present: {has_env}; "
           f"missing report lines: {missing or 'none'}; traceback printed: {'Traceback' in out + err}")


def check_targeted(name, args):
    code, summary = pytest(args)
    record(name, code == 0 and "no tests ran" not in summary, summary)


def check_real_api():
    proc = subprocess.run([PY, "main.py", "--check-claude"], cwd=ROOT,
                          capture_output=True, text=True, timeout=600)
    last_line = proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else "no output"
    record("Health check with the real key in .env", proc.returncode == 0,
           f"exit code {proc.returncode}; {last_line}")
    code, summary = pytest(["-k", "real"], env=dict(os.environ, RUN_REAL_CLAUDE_TEST="1"))
    record("Opt-in real API tests", code == 0, summary)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Phase 0 pass/fail checklist.")
    parser.add_argument("--fast", action="store_true", help="skip the targeted re-runs of test subsets")
    parser.add_argument("--with-real-api", action="store_true",
                        help="also run the real-key checks (makes real Claude requests; costs a few tokens)")
    args = parser.parse_args(argv)

    print("Phase 0 checklist (docs/step4 Section 3)")
    print("=" * 70)
    check_pinned_dependencies()
    check_test_suite()
    check_network_isolated_tests()
    check_startup()
    if not args.fast:
        for name, pytest_args in TARGETED:
            check_targeted(name, pytest_args)
    if args.with_real_api:
        check_real_api()

    print("\nPENDING - these need you, not the script:")
    for name, how in PENDING:
        print(f"[PENDING] {name}\n        {how}")

    failed = [name for name, ok in results if not ok]
    print("\n" + "=" * 70)
    print(f"{len(results) - len(failed)}/{len(results)} automated checks passed; "
          f"{len(PENDING)} item(s) pending a human.")
    if failed:
        print("FAILED: " + "; ".join(failed))
    print("Phase 0 automated checks: " + ("PASS" if not failed else "FAIL"))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
