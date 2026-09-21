"""
Download the speech-recognition model - the ONE intentional model download in this project.

    python scripts/fetch_voice_model.py                 # the model named by listener.model_size
    python scripts/fetch_voice_model.py --model small   # a specific size

Normal use never downloads: listener.local_files_only must be true, and the assistant only ever looks
in the project's model folder (listener.model_dir, a Hugging Face cache, git-ignored). This script is
how a model gets there. It says plainly that it is about to use the network, downloads into that
folder, checks every file the model needs arrived, and then proves the model loads OFFLINE - the same
way the assistant will load it. Nothing else (not app start-up, not pytest) runs it.

Exit codes: 0 downloaded and verified - 1 download or verification failed - 2 bad settings or
arguments - 130 interrupted with Ctrl+C (anything already downloaded is kept and resumed next time).
"""
import argparse
import dataclasses
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.listener import adapter, logic  # noqa: E402
from app.listener.models import VoiceFailure  # noqa: E402
from config.settings import PROJECT_ROOT, SettingsError  # noqa: E402

OK, FAILED, BAD_INPUT, INTERRUPTED = 0, 1, 2, 130


def shown(path: Path) -> str:
    """The destination as the user would type it - relative to the project when it is inside it."""
    try:
        return str(path.relative_to(PROJECT_ROOT))
    except ValueError:
        return str(path)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Download the speech model for voice input.")
    parser.add_argument("--model", choices=logic.MODEL_SIZES,
                        help="model size to download (default: listener.model_size from config.yaml)")
    args = parser.parse_args(argv)
    try:
        settings = logic.listener_settings()
    except SettingsError as exc:
        print(f"Can't read the listener settings: {exc}")
        return BAD_INPUT
    size = args.model or settings.model_size
    destination = shown(logic.model_root(settings.model_dir))

    print(f"About to DOWNLOAD the faster-whisper '{size}' speech model from huggingface.co")
    print(f"into {destination} (this uses the network; the model is several hundred MB).")
    try:
        fetched = adapter.fetch_model(size, settings.model_dir)
    except KeyboardInterrupt:
        print("\nInterrupted. Anything already downloaded is kept in the model folder and will be "
              "resumed next time you run this.")
        return INTERRUPTED
    if isinstance(fetched, VoiceFailure):
        print(fetched.message)
        return FAILED
    on_disk = sum(entry.stat().st_size for entry in Path(fetched).iterdir() if entry.is_file())
    print(f"Downloaded: every file the model needs is present ({on_disk / 1_000_000:.0f} MB).")

    print("Checking it loads OFFLINE, the way the assistant will load it...")
    status = adapter.ensure_model(dataclasses.replace(settings, model_size=size))
    if isinstance(status, VoiceFailure):
        print(f"The download finished, but the model did not load: {status.message}")
        return FAILED
    note = f" (CUDA failed: {status.fallback_reason}; running on CPU)" if status.fell_back_from else ""
    print(f"Ready: '{status.model_size}' loads offline on {status.device} ({status.compute_type}) in "
          f"{status.load_seconds:.1f} s{note}.")
    if size != settings.model_size:
        print(f"Note: listener.model_size is still '{settings.model_size}' - change it to use '{size}'.")
    return OK


if __name__ == "__main__":
    sys.exit(main())
