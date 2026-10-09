"""Writing a file Leyline keeps in the repository (leyline.md, the learnings and anchors files) whole or not at all."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path


def write_text(path: Path, text: str) -> None:
    """Write `text` to a file beside `path` and move it into place, so a write that fails part way (a full disk, a
    killed process) leaves the file as it was rather than cut off, and a reader never sees half of it."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        os.chmod(tmp, path.stat().st_mode & 0o777 if path.exists() else 0o644)   # a temp file is private; this is not
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
