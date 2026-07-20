from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProductionSettings(StrictModel):
    name: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
    output_dir: Path
    jobs: int = Field(gt=0)
    generator_calls_per_job: int = Field(gt=0)
    base_seed: int = Field(ge=1, lt=2_147_000_000)
    seed_stride: int = Field(default=10, ge=3)


class DLPGeneratorSource(StrictModel):
    type: Literal["dlpgen"]
    config: Path
    executable: str = "dlpgen"
    expected_commit: str | None = None


class StageSoftware(StrictModel):
    executable: str
    expected_commit: str | None = None


class PinnedDependency(StrictModel):
    expected_commit: str


class SoftwareSettings(StrictModel):
    container_image: str
    edep_sim: StageSoftware
    edep2supera: StageSoftware
    supera_atomic: PinnedDependency

    @model_validator(mode="after")
    def immutable_container_tag(self) -> "SoftwareSettings":
        if self.container_image.endswith(":latest"):
            raise ValueError("container_image must not use the mutable 'latest' tag")
        return self


class DetectorSettings(StrictModel):
    geometry: Path
    supera_config: Path
    physics_list: str = "QGSP_BERT"


class ExecutionSettings(StrictModel):
    resume: bool = True


class ProductionConfig(StrictModel):
    schema_version: Literal[1]
    production: ProductionSettings
    source: DLPGeneratorSource
    software: SoftwareSettings
    detector: DetectorSettings
    execution: ExecutionSettings = ExecutionSettings()
    config_path: Path = Field(exclude=True)

    @model_validator(mode="after")
    def ensure_unique_seed_ranges(self) -> "ProductionConfig":
        maximum = self.production.base_seed + (
            (self.production.jobs - 1) * self.production.seed_stride + 2
        )
        if maximum >= 2_147_000_000:
            raise ValueError("derived seeds exceed the supported signed integer range")
        return self

    def seed(self, job: int, stage_offset: int) -> int:
        return self.production.base_seed + job * self.production.seed_stride + stage_offset

    def resolved_dict(self) -> dict:
        return self.model_dump(mode="json", exclude={"config_path"})


def _resolve(path: Path, base: Path) -> Path:
    expanded = path.expanduser()
    return (base / expanded).resolve() if not expanded.is_absolute() else expanded.resolve()


def load_config(path: str | Path) -> ProductionConfig:
    config_path = Path(path).expanduser().resolve()
    with config_path.open(encoding="utf-8") as stream:
        raw = yaml.safe_load(stream)
    if not isinstance(raw, dict):
        raise ValueError("production configuration must contain a YAML mapping")

    base = config_path.parent
    raw.setdefault("production", {})
    raw.setdefault("source", {})
    raw.setdefault("detector", {})
    for section, key in (
        ("production", "output_dir"),
        ("source", "config"),
        ("detector", "geometry"),
        ("detector", "supera_config"),
    ):
        if key in raw[section]:
            raw[section][key] = _resolve(Path(raw[section][key]), base)
    raw["config_path"] = config_path
    return ProductionConfig.model_validate(raw)
