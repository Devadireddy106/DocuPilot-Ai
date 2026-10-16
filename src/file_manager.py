"""Safe file handling: filename sanitising, private temp workspaces, unique output paths."""
from __future__ import annotations

import hashlib
import logging
import re
import shutil
import tempfile
import time
import unicodedata
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Union

log = logging.getLogger(__name__)

_RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}
OUTPUT_PREFIX = "docupilot_"


def sanitize_filename(name: object, default: str = "document", max_len: int = 60) -> str:
    """Return a conservative, filesystem-safe *stem* (no directory, no extension)."""
    raw = str(name or "").replace("\\", "/").split("/")[-1]
    stem = Path(raw).stem
    stem = unicodedata.normalize("NFKD", stem).encode("ascii", "ignore").decode("ascii")
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", stem)
    stem = re.sub(r"_+", "_", stem).strip("._-")
    if not stem:
        stem = default
    if stem.lower() in _RESERVED:
        stem = "_" + stem
    return stem[:max_len]


def display_name(name: object, max_len: int = 120) -> str:
    """Name to show to the user / write into a workbook: base name only, control chars removed."""
    raw = str(name or "").replace("\\", "/").split("/")[-1]
    raw = re.sub(r"[\x00-\x1f\x7f]", "", raw).strip()
    return (raw or "document.pdf")[:max_len]


@contextmanager
def workspace(prefix: str = "docupilot_work_") -> Iterator[Path]:
    """A private temporary directory that is always removed afterwards."""
    d = Path(tempfile.mkdtemp(prefix=prefix))
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


def sha256_file(path: Union[str, Path], chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


def stage_upload(src: Union[str, Path], workdir: Path) -> Path:
    """Copy an uploaded file into the private workspace under a fixed, safe name."""
    dst = workdir / "input.pdf"
    shutil.copyfile(src, dst)
    return dst


def unique_path(directory: Path, base: str, suffix: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    cand = directory / f"{base}{suffix}"
    i = 1
    while cand.exists():
        cand = directory / f"{base}_{i}{suffix}"
        i += 1
    return cand


def publish_file(tmp_file: Path, directory: Path, base: str, suffix: str) -> Path:
    """Move a finished temp file into the output directory without overwriting anything."""
    target = unique_path(directory, base, suffix)
    shutil.move(str(tmp_file), str(target))
    return target


def purge_old_outputs(directory: Path, max_age_hours: float = 24.0) -> int:
    """Delete OUR OWN old output files (prefix `docupilot_`) from the output directory.

    Files without the prefix are never touched.
    """
    removed = 0
    if not directory.exists():
        return 0
    cutoff = time.time() - max_age_hours * 3600
    for p in directory.iterdir():
        try:
            if (
                p.is_file()
                and p.name.startswith(OUTPUT_PREFIX)
                and p.suffix.lower() in {".xlsx", ".csv", ".json"}
                and p.stat().st_mtime < cutoff
            ):
                p.unlink()
                removed += 1
        except OSError as exc:  # pragma: no cover - best effort
            log.warning("Could not purge %s: %s", p, exc)
    return removed
