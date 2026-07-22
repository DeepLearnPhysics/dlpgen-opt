from __future__ import annotations

from pathlib import Path
from typing import Annotated, Literal

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
    checkout: Path | None = None
    expected_commit: str | None = None


class GenieFluxSettings(StrictModel):
    file_pattern: Path
    distance_m: float = Field(gt=0)
    center_m: tuple[float, float] = (0.0, 0.0)
    window_size_m: tuple[float, float] = (1.0, 1.0)
    flavors: list[Literal[12, -12, 14, -14]] = Field(
        default_factory=lambda: [12, -12, 14, -14], min_length=1
    )
    max_energy_gev: float = Field(default=20.0, gt=0)
    max_weight_scan_entries: int = Field(default=250_000, gt=0)
    checksum_files: bool = False
    stage_to_local: bool = False

    @model_validator(mode="after")
    def valid_window_and_flavors(self) -> "GenieFluxSettings":
        if any(length <= 0 for length in self.window_size_m):
            raise ValueError("window_size_m values must be positive")
        if len(set(self.flavors)) != len(self.flavors):
            raise ValueError("flux flavors must be unique")
        return self


class GenieSource(StrictModel):
    type: Literal["genie"]
    config: Path | None = None
    flux: GenieFluxSettings
    executable: str = "dlpgen-opt-genie"
    gevgen_executable: str = "gevgen_fnal"
    converter_executable: str = "gntpc"
    tune: str = "AR23_20i_00_000"
    spline: Path = Path("/opt/genie/xsec/gxspl-AR23_20i_00_000.xml")
    target_pdg: int = 1_000_180_400
    vertex_cm: tuple[float, float, float] = (0.0, 0.0, 0.0)
    expected_commit: str | None = None
    dk2nu_expected_commit: str | None = None


SourceSettings = Annotated[
    DLPGeneratorSource | GenieSource, Field(discriminator="type")
]


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
    source: SourceSettings
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
        resolved = self.model_dump(mode="json", exclude={"config_path"})
        # Preserve the serialized form of existing standard productions so a
        # new optional development field does not invalidate their manifests.
        if isinstance(self.source, DLPGeneratorSource) and self.source.checkout is None:
            resolved["source"].pop("checkout", None)
        if isinstance(self.source, GenieSource) and self.source.config is None:
            resolved["source"].pop("config", None)
        return resolved


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
        ("detector", "geometry"),
        ("detector", "supera_config"),
    ):
        if key in raw[section]:
            raw[section][key] = _resolve(Path(raw[section][key]), base)
    source = raw["source"]
    if source.get("type") == "dlpgen":
        if "config" in source:
            source["config"] = _resolve(Path(source["config"]), base)
        if source.get("checkout") is not None:
            source["checkout"] = _resolve(Path(source["checkout"]), base)
    if source.get("type") == "genie":
        source_base = base
        if source.get("config") is not None:
            source_config = _resolve(Path(source["config"]), base)
            with source_config.open(encoding="utf-8") as stream:
                settings = yaml.safe_load(stream)
            if not isinstance(settings, dict):
                raise ValueError("GENIE source configuration must contain a YAML mapping")
            source = {**settings, **source, "config": source_config}
            raw["source"] = source
            source_base = source_config.parent
        if "spline" in source:
            source["spline"] = _resolve(Path(source["spline"]), source_base)
        flux = source.setdefault("flux", {})
        if "file_pattern" in flux:
            flux["file_pattern"] = _resolve(Path(flux["file_pattern"]), source_base)
    raw["config_path"] = config_path
    return ProductionConfig.model_validate(raw)
