from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

from ..config import ProductionConfig
from ..layout import JobLayout


class SourceBackend(ABC):
    """A primary-event source that hands HEPEVT-compatible events downstream."""

    @abstractmethod
    def command(self, config: ProductionConfig, job: int, layout: JobLayout) -> list[str]:
        raise NotImplementedError

    @abstractmethod
    def finalize(self, config: ProductionConfig, layout: JobLayout) -> dict[str, object]:
        raise NotImplementedError

    @abstractmethod
    def output(self, layout: JobLayout) -> Path:
        raise NotImplementedError

    @abstractmethod
    def outputs(self, layout: JobLayout) -> list[Path]:
        raise NotImplementedError

    @abstractmethod
    def inputs(self, config: ProductionConfig) -> list[Path]:
        raise NotImplementedError

    @abstractmethod
    def edep_macro_lines(
        self, config: ProductionConfig, layout: JobLayout
    ) -> list[str]:
        raise NotImplementedError
