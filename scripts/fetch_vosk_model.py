"""
Download the Vosk speech model for the spoken-stop PROTOTYPE - the only code allowed to fetch it.

    python scripts/fetch_vosk_model.py

Normal use never downloads: nothing in app/ or tests/ reaches the network for this model, and the
benchmark refuses to run if the model is not already on disk. This script is how it gets there. It says
plainly that it is about to use the network, downloads the official upstream archive, checks it against
the checksum upstream itself publishes, records its SHA-256 too, extracts it into data/models/vosk/
(git-ignored), and then lists and validates the files a local recognizer actually needs.

Why this script exists at all: `vosk.Model(lang=...)` or `Model(model_name=...)` DOWNLOADS a model over
the network by itself, into ~/AppData/Local/vosk, using requests and urlretrieve that are imported the
moment `vosk` is imported. This project never uses those code paths - the benchmark always passes an
explicit local `model_path`. This script is the one deliberate, visible download.

Honesty about the checksum: upstream publishes an MD5 for each model in its own model-list.json, and
this script compares against it, so a truncated or corrupted download is caught. It is NOT a signature
and MD5 is not collision-resistant, and the checksum is served from the same host over the same
connection as the archive - so this proves the bytes arrived intact, not that upstream is who we think
it is. The SHA-256 printed alongside is OUR OWN measurement, for reproducing the same bytes later.

This is a prototype evaluation (Task 6d2a). vosk is not a permanent project dependency, and nothing in
app/ imports it.

Exit codes: 0 downloaded and validated - 1 download or validation failed - 2 bad arguments -
130 interrupted with Ctrl+C (a partial archive is removed so the next run starts cleanly).
"""
import argparse
import hashlib
import sys
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.settings import PROJECT_ROOT  # noqa: E402

OK, FAILED, BAD_INPUT, INTERRUPTED = 0, 1, 2, 130

MODEL = "vosk-model-small-en-us-0.15"
# The official upstream location, exactly as upstream's own model-list.json gives it.
ARCHIVE_URL = f"https://alphacephei.com/vosk/models/{MODEL}.zip"
EXPECTED_MD5 = "09ab50ccd62b674cbaa231b825f9c1cb"   # upstream-published, from model-list.json
EXPECTED_BYTES = 41_205_931                        # upstream-published size of the archive
VOSK_ROOT = PROJECT_ROOT / "data" / "models" / "vosk"

# What a local Vosk recognizer needs: the acoustic model, both config files the model loader reads,
# and the decoding graph. `vosk_model_new` reads conf/model.conf for, among other things, the
# endpointing rules, so a model without it would silently fall back to different behaviour.
REQUIRED = ("am/final.mdl", "conf/mfcc.conf", "conf/model.conf",
            "graph/HCLr.fst", "graph/Gr.fst", "graph/phones/word_boundary.int",
            "graph/disambig_tid.int")


def shown(path: Path) -> str:
    try:
        return str(path.relative_to(PROJECT_ROOT))
    except ValueError:
        return str(path)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Download the Vosk speech model for the spoken-stop prototype.")
    parser.add_argument("--force", action="store_true",
                        help="download again even if the model is already there")
    try:
        arguments = parser.parse_args(argv)
    except SystemExit:
        return BAD_INPUT

    folder = VOSK_ROOT / MODEL
    if folder.is_dir() and not arguments.force:
        print(f"Already there: {shown(folder)}")
        return _validate(folder)

    print(f"About to DOWNLOAD the Vosk model '{MODEL}' from alphacephei.com")
    print(f"into {shown(VOSK_ROOT)} (this uses the network; the archive is about 39 MiB).")
    archive = VOSK_ROOT / f"{MODEL}.zip"
    VOSK_ROOT.mkdir(parents=True, exist_ok=True)
    try:
        digests, size = _download(ARCHIVE_URL, archive)
    except KeyboardInterrupt:
        archive.unlink(missing_ok=True)
        print("\nInterrupted; the partial download was removed.")
        return INTERRUPTED
    except (urllib.error.URLError, OSError) as exc:
        archive.unlink(missing_ok=True)
        print(f"Download failed ({type(exc).__name__}). Check the internet connection and try again.")
        return FAILED

    print(f"Downloaded {size / (1 << 20):.1f} MiB ({size} bytes)")
    if size != EXPECTED_BYTES:
        print(f"  size MISMATCH: upstream publishes {EXPECTED_BYTES} bytes")
    print(f"MD5    : {digests['md5']}")
    if digests["md5"] != EXPECTED_MD5:
        archive.unlink(missing_ok=True)
        print(f"  MISMATCH - upstream publishes {EXPECTED_MD5}")
        print("  The archive was removed. Nothing was extracted.")
        return FAILED
    print("  matches the MD5 upstream publishes in model-list.json (integrity, not a signature)")
    print(f"SHA-256: {digests['sha256']}")
    print("  (our own measurement, for reproducing the same bytes later)")

    try:
        _extract(archive, VOSK_ROOT)
    except (zipfile.BadZipFile, ValueError, OSError) as exc:
        print(f"The archive could not be extracted ({type(exc).__name__}: {exc}).")
        return FAILED
    finally:
        archive.unlink(missing_ok=True)   # the extracted folder is what we keep
    return _validate(folder)


def _download(url: str, destination: Path):
    """Stream the archive to disk, hashing it as it arrives. Nothing is held in memory."""
    md5, sha256 = hashlib.md5(), hashlib.sha256()
    size = reported = 0
    with urllib.request.urlopen(url, timeout=120) as response, destination.open("wb") as out:
        while True:
            block = response.read(1 << 16)
            if not block:
                break
            md5.update(block)
            sha256.update(block)
            out.write(block)
            size += len(block)
            if size - reported >= 8_000_000:  # a line every few MiB, so a piped log stays readable
                reported = size
                print(f"  {size / (1 << 20):6.1f} MiB", flush=True)
    return {"md5": md5.hexdigest(), "sha256": sha256.hexdigest()}, size


def _extract(archive: Path, into: Path) -> None:
    """Extract, refusing any member that would write outside `into`.

    zipfile has no equivalent of tarfile's filter="data", so absolute paths, drive letters and ..
    segments are checked here rather than trusted."""
    into = into.resolve()
    with zipfile.ZipFile(archive) as zipped:
        for member in zipped.infolist():
            name = member.filename
            if name.startswith(("/", "\\")) or ".." in Path(name).parts or Path(name).drive:
                raise ValueError(f"the archive contains an unsafe path: {name!r}")
            target = (into / name).resolve()
            if not str(target).startswith(str(into)):
                raise ValueError(f"the archive would write outside the model folder: {name!r}")
        zipped.extractall(into)


def _validate(folder: Path) -> int:
    """List what arrived and check the files a local recognizer needs are all present."""
    if not folder.is_dir():
        print(f"The model folder is missing: {shown(folder)}")
        return FAILED
    files = sorted(path for path in folder.rglob("*") if path.is_file())
    total = sum(path.stat().st_size for path in files)
    print(f"\nContents of {shown(folder)} ({len(files)} files, {total / (1 << 20):.1f} MiB extracted):")
    for path in files:
        print(f"  {path.relative_to(folder).as_posix():<44} {path.stat().st_size / (1 << 20):8.2f} MiB")

    present = {path.relative_to(folder).as_posix() for path in files}
    missing = [name for name in REQUIRED if name not in present]
    if missing:
        print(f"\nIncomplete: missing {', '.join(missing)}")
        print("Run this script again with --force.")
        return FAILED
    empty = [name for name in REQUIRED if (folder / name).stat().st_size == 0]
    if empty:
        print(f"\nIncomplete: empty {', '.join(empty)}")
        return FAILED
    print(f"\nReady: every file a local recognizer needs is present in {shown(folder)}.")
    print("Nothing else downloads this model; the benchmark only reads it, from this explicit path.")
    configuration = folder / "conf" / "model.conf"
    print(f"\n{shown(configuration)} (it sets the endpointing rules the benchmark measures):")
    for line in configuration.read_text(encoding="utf-8").splitlines():
        if line.strip():
            print(f"  {line.strip()}")
    return OK


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nInterrupted.")
        sys.exit(INTERRUPTED)
