"""Bootstrap: assemble main from _main_parts then exec (temporary upload workaround)."""
from __future__ import annotations

from pathlib import Path

_PARTS_DIR = Path(__file__).resolve().parent / "_main_parts"
_parts = sorted(_PARTS_DIR.glob("part_*.txt"))
if not _parts:
    raise RuntimeError(f"Missing main parts in {_PARTS_DIR}")
_source = "".join(path.read_text(encoding="utf-8") for path in _parts)
exec(compile(_source, str(Path(__file__).resolve()), "exec"), globals())
