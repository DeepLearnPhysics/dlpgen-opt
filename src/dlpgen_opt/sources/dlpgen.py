from __future__ import annotations

import csv
from collections import defaultdict
from pathlib import Path

from ..config import ProductionConfig
from ..layout import JobLayout
from ..validation import validate_nonempty, validate_source_csv
from .base import SourceBackend


class DLPGeneratorBackend(SourceBackend):
    def command(self, config: ProductionConfig, job: int, layout: JobLayout) -> list[str]:
        return [
            config.source.executable,
            str(config.source.config),
            str(config.production.generator_calls_per_job),
            "--seed",
            str(config.seed(job, 0)),
            "--format",
            "csv",
            "--output",
            str(layout.source_csv),
        ]

    def output(self, layout: JobLayout) -> Path:
        return layout.hepevt

    def outputs(self, layout: JobLayout) -> list[Path]:
        return [layout.source_csv, layout.hepevt]

    def inputs(self, config: ProductionConfig) -> list[Path]:
        return [config.source.config]

    def edep_macro_lines(
        self, config: ProductionConfig, layout: JobLayout
    ) -> list[str]:
        return [
            "/generator/kinematics/hepevt/input " + str(layout.hepevt),
            "/generator/kinematics/hepevt/flavor pbomb",
            "/generator/kinematics/hepevt/verbose 0",
            "/generator/kinematics/set hepevt",
        ]

    def finalize(self, config: ProductionConfig, layout: JobLayout) -> dict[str, object]:
        csv_result = validate_source_csv(
            layout.source_csv, config.production.generator_calls_per_job
        )
        with layout.source_csv.open(newline="", encoding="utf-8") as stream:
            rows = list(csv.DictReader(stream))

        grouped: dict[tuple[int, int], list[dict[str, str]]] = defaultdict(list)
        interactions_by_call: dict[int, set[int]] = defaultdict(set)
        for row in rows:
            call = int(row["call_id"])
            interaction = int(row["interaction_id"])
            grouped[(call, interaction)].append(row)
            interactions_by_call[call].add(interaction)

        invalid = {call: ids for call, ids in interactions_by_call.items() if len(ids) != 1}
        if invalid:
            raise RuntimeError(
                "the upstream HEPEVT reader cannot preserve multiple DLPGenerator "
                "vertices in one detector-simulation event; configure NumEvent: [1, 1] "
                f"for the initial pipeline (violations: {invalid})"
            )

        integer_fields = (
            "status_code", "pdg_code", "parent0", "parent1", "child_first",
            "child_last",
        )
        real_fields = ("px", "py", "pz", "energy", "mass")
        with layout.hepevt.open("w", encoding="utf-8") as output:
            for (call, interaction), particles in sorted(grouped.items()):
                first = particles[0]
                # DLPGenerator positions are millimetres; edep-sim's extended
                # HEPEVT vertex header is explicitly centimetres.
                x_cm, y_cm, z_cm = (float(first[key]) / 10.0 for key in ("x", "y", "z"))
                output.write(
                    f"{call} {interaction} {len(particles)} "
                    f"{x_cm:.12g} {y_cm:.12g} {z_cm:.12g} {first['t']}\n"
                )
                for particle in particles:
                    # DLPGenerator's CSV writer may render integral identifiers
                    # as e.g. ``3.0``; HEPEVT's parser requires lexical integers.
                    integers = [str(int(float(particle[field]))) for field in integer_fields]
                    reals = [particle[field] for field in real_fields]
                    output.write(" ".join(integers + reals) + "\n")

        result = validate_nonempty(layout.hepevt)
        result.update({"source_csv": csv_result, "format": "edep-sim-pbomb"})
        return result
