from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class InputArtifact:
    """A stage input with an explicit payload-checksum policy."""

    path: Path
    checksum: bool = True
