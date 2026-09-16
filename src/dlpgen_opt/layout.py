from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .config import ProductionConfig


@dataclass(frozen=True)
class JobLayout:
    root: Path
    source_dir: Path
    edep_dir: Path
    supera_dir: Path
    spine_dir: Path
    logs_dir: Path

    @classmethod
    def for_job(cls, config: ProductionConfig, job: int) -> "JobLayout":
        root = config.production.output_dir / "jobs" / f"{job:05d}"
        return cls(
            root=root,
            source_dir=root / "source",
            edep_dir=root / "edep-sim",
            supera_dir=root / "supera",
            spine_dir=root / "spine",
            logs_dir=root / "logs",
        )

    def create(self) -> None:
        for directory in (
            self.root,
            self.source_dir,
            self.edep_dir,
            self.supera_dir,
            self.spine_dir,
            self.logs_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)

    @property
    def source_csv(self) -> Path:
        return self.source_dir / "events.csv"

    @property
    def hepevt(self) -> Path:
        return self.source_dir / "events.pbomb.hepevt"

    @property
    def source_conversion_metadata(self) -> Path:
        return self.source_dir / "nuhepmc-conversion.json"

    @property
    def gibuu_native_archive(self) -> Path:
        return self.source_dir / "gibuu-native-candidates.tar.gz"

    @property
    def gibuu_jobcard(self) -> Path:
        return self.source_dir / "gibuu-resolved-jobcards.yaml"

    @property
    def nuwro_native(self) -> Path:
        return self.source_dir / "nuwro-events.root"

    @property
    def nuwro_params(self) -> Path:
        return self.source_dir / "nuwro-params.txt"

    @property
    def neut_native_archive(self) -> Path:
        return self.source_dir / "neut-native.tar.gz"

    @property
    def neut_cards(self) -> Path:
        return self.source_dir / "neut-resolved-cards.yaml"

    @property
    def genie_flux_config(self) -> Path:
        return self.source_dir / "dk2nu-flux.xml"

    @property
    def genie_ghep(self) -> Path:
        return self.source_dir / f"events.{int(self.root.name)}.ghep.root"

    @property
    def rootracker(self) -> Path:
        return self.source_dir / "events.gtrac.root"

    @property
    def edep_macro(self) -> Path:
        return self.edep_dir / "run.mac"

    @property
    def edep_output(self) -> Path:
        return self.edep_dir / "edep.root"

    @property
    def supera_output(self) -> Path:
        return self.supera_dir / "supera.root"

    @property
    def resolved_supera_config(self) -> Path:
        return self.supera_dir / "config.yaml"

    @property
    def spine_output(self) -> Path:
        return self.spine_dir / "spine.h5"

    def status(self, stage: str) -> Path:
        return self.root / f"{stage}.yaml"
