"""
Fresh-clone verification (docs/step4 Section 3: "main.py runs cleanly on a fresh clone") -
repeatable.

    python scripts/fresh_clone_check.py [--keep]

Copies exactly what a clone would contain (git ls-files: tracked files plus new files that
aren't git-ignored) into a temporary folder, builds a fresh virtual environment from the
pinned requirements.txt, then runs the test suite and main.py there. Nothing from this
project's venv/, data/, logs/ or .env is copied. pip downloads from PyPI; no Claude API
calls are made.
"""
import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
results = []


def record(name, ok, detail):
    results.append((name, ok))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}\n        {detail}", flush=True)


def clone_file_list():
    proc = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard"],
                          cwd=ROOT, capture_output=True, text=True, check=True)
    return [line for line in proc.stdout.splitlines() if line.strip()]


def copy_clone(target: Path):
    files = clone_file_list()
    for name in files:
        source, destination = ROOT / name, target / name
        if not source.is_file():
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
    return files


def run(cmd, cwd, timeout):
    print(f"    $ {' '.join(str(c) for c in cmd)}", flush=True)
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)


def verify(clone: Path):
    files = copy_clone(clone)
    leaked = [n for n in files if n.startswith(("venv/", "data/", "logs/")) and not n.endswith(".gitkeep")]
    record("Clone contains only committed project files",
           bool(files) and not leaked and not (clone / ".env").exists(),
           f"{len(files)} files copied; unexpected: {leaked or 'none'}; .env copied: {(clone / '.env').exists()}")

    venv = clone / "venv"
    created = run([sys.executable, "-m", "venv", str(venv)], clone, 600)
    python = venv / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    record("Fresh virtual environment created", created.returncode == 0 and python.exists(),
           created.stderr.strip()[-300:] or f"{python}")
    if not python.exists():
        return

    install = run([str(python), "-m", "pip", "install", "--disable-pip-version-check",
                   "-r", "requirements.txt"], clone, 3600)
    record("Pinned requirements install cleanly", install.returncode == 0,
           (install.stdout.strip().splitlines() or ["no output"])[-1][:300]
           if install.returncode == 0 else install.stderr.strip()[-500:])

    tests = run([str(python), "-m", "pytest", "-q", "-p", "no:cacheprovider"], clone, 1800)
    summary = next((line.strip() for line in reversed(tests.stdout.splitlines())
                    if "passed" in line or "failed" in line or "error" in line), "no summary")
    record("Test suite passes in the fresh clone", tests.returncode == 0, summary)

    startup = run([str(python), "main.py"], clone, 600)
    expected = ["API key", "Cost limits", "Model price", "Usage ledger", "Safety rules", "Logging"]
    missing = [name for name in expected if f"] {name}:" not in startup.stdout]
    record("main.py runs cleanly in the fresh clone (no .env, so the API key must fail)",
           startup.returncode == 1 and not missing
           and "Traceback" not in startup.stdout + startup.stderr
           and "[FAIL] API key" in startup.stdout,
           f"exit code {startup.returncode}; missing report lines: {missing or 'none'}")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Verify a fresh clone installs and passes.")
    parser.add_argument("--keep", action="store_true", help="keep the temporary clone for inspection")
    args = parser.parse_args(argv)

    clone = Path(tempfile.mkdtemp(prefix="companion-fresh-clone-"))
    print(f"Fresh-clone verification in {clone}\n" + "=" * 60)
    try:
        verify(clone)
    finally:
        if args.keep:
            print(f"\nClone kept at {clone}")
        else:
            shutil.rmtree(clone, ignore_errors=True)

    failed = [name for name, ok in results if not ok]
    print("\n" + "=" * 60)
    print(f"{len(results) - len(failed)}/{len(results)} checks passed")
    if failed:
        print("FAILED: " + "; ".join(failed))
    print("Fresh clone: " + ("PASS" if not failed else "FAIL"))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
