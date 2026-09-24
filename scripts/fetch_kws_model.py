"""
Download the keyword-spotting model for the spoken-stop PROTOTYPE - the only code allowed to fetch it.

    python scripts/fetch_kws_model.py

Normal use never downloads: nothing in app/ or tests/ reaches the network for this model, and the
benchmark refuses to run if the model is not already on disk. This script is how it gets there. It
says plainly that it is about to use the network, downloads the official upstream archive, records its
SHA-256, extracts it into data/models/kws/ (git-ignored), and then lists and validates the files a CPU
keyword spotter actually needs.

Honesty about the hash: upstream publishes these models as plain GitHub release assets with no
checksum or signature that this script can compare against. The SHA-256 it prints is OUR OWN
measurement of what we received - useful for reproducing the same bytes later, and NOT a verification
of upstream authenticity.

This is a prototype evaluation (Task 6d1a). sherpa-onnx is not a permanent project dependency yet, and
nothing in app/ imports it.

Exit codes: 0 downloaded and validated - 1 download or validation failed - 2 bad arguments -
130 interrupted with Ctrl+C (a partial archive is removed so the next run starts cleanly).
"""
import argparse
import hashlib
import shutil
import sys
import tarfile
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.settings import PROJECT_ROOT  # noqa: E402

OK, FAILED, BAD_INPUT, INTERRUPTED = 0, 1, 2, 130

MODEL = "sherpa-onnx-kws-zipformer-gigaspeech-3.3M-2024-01-01"
ARCHIVE_URL = (f"https://github.com/k2-fsa/sherpa-onnx/releases/download/kws-models/"
               f"{MODEL}.tar.bz2")
KWS_ROOT = PROJECT_ROOT / "data" / "models" / "kws"

# What a CPU keyword spotter needs. The int8 encoder/decoder/joiner triple is the pairing the official
# example uses for this model; the fp32 files come in the same archive and are left in place.
REQUIRED = ("tokens.txt", "bpe.model",
            "encoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx",
            "decoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx",
            "joiner-epoch-12-avg-2-chunk-16-left-64.int8.onnx")


def shown(path: Path) -> str:
    try:
        return str(path.relative_to(PROJECT_ROOT))
    except ValueError:
        return str(path)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Download the keyword-spotting model for the spoken-stop prototype.")
    parser.add_argument("--force", action="store_true",
                        help="download again even if the model is already there")
    try:
        arguments = parser.parse_args(argv)
    except SystemExit:
        return BAD_INPUT

    folder = KWS_ROOT / MODEL
    if folder.is_dir() and not arguments.force:
        print(f"Already there: {shown(folder)}")
        return _validate(folder)

    print(f"About to DOWNLOAD the keyword-spotting model '{MODEL}' from github.com")
    print(f"into {shown(KWS_ROOT)} (this uses the network; the archive is about 19 MB).")
    archive = KWS_ROOT / f"{MODEL}.tar.bz2"
    KWS_ROOT.mkdir(parents=True, exist_ok=True)
    try:
        digest, size = _download(ARCHIVE_URL, archive)
    except KeyboardInterrupt:
        archive.unlink(missing_ok=True)
        print("\nInterrupted; the partial download was removed.")
        return INTERRUPTED
    except (urllib.error.URLError, OSError) as exc:
        archive.unlink(missing_ok=True)
        print(f"Download failed ({type(exc).__name__}). Check the internet connection and try again.")
        return FAILED

    print(f"Downloaded {size / 1_000_000:.1f} MB")
    print(f"SHA-256 of what we received: {digest}")
    print("  (our own measurement - upstream publishes no checksum for this asset, so this is for")
    print("   reproducing the same bytes later, NOT proof of upstream authenticity.)")

    try:
        _extract(archive, KWS_ROOT)
    except (tarfile.TarError, OSError) as exc:
        print(f"The archive could not be extracted ({type(exc).__name__}).")
        return FAILED
    finally:
        archive.unlink(missing_ok=True)   # the extracted folder is what we keep
    return _validate(folder)


def _download(url: str, destination: Path):
    """Stream the archive to disk, hashing it as it arrives. Nothing is held in memory."""
    digest = hashlib.sha256()
    size = reported = 0
    with urllib.request.urlopen(url, timeout=120) as response, destination.open("wb") as out:
        while True:
            block = response.read(1 << 16)
            if not block:
                break
            digest.update(block)
            out.write(block)
            size += len(block)
            if size - reported >= 4_000_000:  # a line every few MB, so a piped log stays readable
                reported = size
                print(f"  {size / 1_000_000:6.1f} MB", flush=True)
    return digest.hexdigest(), size


def _extract(archive: Path, into: Path) -> None:
    """Extract with the 'data' filter, so nothing in the archive can write outside `into`."""
    with tarfile.open(archive, "r:bz2") as tar:
        tar.extractall(path=into, filter="data")


def _validate(folder: Path) -> int:
    """List what arrived and check the files a CPU keyword spotter needs are all present."""
    if not folder.is_dir():
        print(f"The model folder is missing: {shown(folder)}")
        return FAILED
    files = sorted(path for path in folder.rglob("*") if path.is_file())
    print(f"\nContents of {shown(folder)}:")
    for path in files:
        print(f"  {path.relative_to(folder).as_posix():<60} {path.stat().st_size / 1_000_000:7.2f} MB")

    names = {path.name for path in files}
    missing = [name for name in REQUIRED if name not in names]
    if missing:
        print(f"\nIncomplete: missing {', '.join(missing)}")
        print("Run this script again with --force.")
        return FAILED
    empty = [name for name in REQUIRED if (folder / name).stat().st_size == 0]
    if empty:
        print(f"\nIncomplete: empty {', '.join(empty)}")
        return FAILED
    print(f"\nReady: every file the CPU keyword spotter needs is present in {shown(folder)}.")
    print("Nothing else downloads this model; the benchmark only reads it.")
    return OK


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nInterrupted.")
        sys.exit(INTERRUPTED)
