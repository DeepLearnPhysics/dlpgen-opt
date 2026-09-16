from __future__ import annotations

from array import array
from dataclasses import dataclass
import math
from pathlib import Path
import re


NEUTRINO_PDGS = {12, 14, 16}
NUCLEAR_REMNANT_PDGS = {200_990_000, 2_009_900_000}
MAX_PARTICLES = 4000


@dataclass(frozen=True)
class ParticleRecord:
    pdg: int
    status: int
    momentum_gev: tuple[float, float, float, float]


@dataclass(frozen=True)
class RooTrackerEvent:
    event_number: int
    process_id: int
    reaction: str
    vertex_m: tuple[float, float, float, float]
    particles: tuple[ParticleRecord, ...]
    nucleon_pdg: int = -1
    cross_section_1e38_cm2: float = 0.0
    differential_cross_section_1e38_cm2: float = 0.0
    weight: float = 1.0
    probability: float = 1.0


def _attribute_text(attribute: object) -> str:
    return str(attribute).strip()


def _process_name(event: object, process_id: int) -> str:
    run_info = event.run_info
    if run_info is None:
        return "Unknown"
    key = f"NuHepMC.ProcessInfo[{process_id}].Name"
    attribute = run_info.attributes[key] if key in run_info.attributes else None
    return _attribute_text(attribute) if attribute is not None else "Unknown"


def _reaction_mode(process_name: str) -> tuple[str, str]:
    name = process_name.upper()
    if name.startswith("CC") or re.search(r"\bCC\b", name):
        current = "Weak[CC]"
    elif name.startswith("NC") or re.search(r"\bNC\b", name):
        current = "Weak[NC]"
    else:
        current = "Weak[Unknown]"
    if "2P2H" in name or "MEC" in name:
        mode = "MEC"
    elif "QE" in name or "ELASTIC" in name:
        mode = "QES"
    elif "DIS" in name:
        mode = "DIS"
    elif "RES" in name:
        mode = "RES"
    elif "COH" in name:
        mode = "COH"
    elif "DIF" in name:
        mode = "DFR"
    elif any(token in name for token in ("ETA", "KAON", "GAMMA", "MULTI_PI")):
        mode = "RES"
    elif "PION" in name or "BKGD" in name:
        mode = "1Pion"
    else:
        mode = "Unknown"
    return current, mode


def _momentum(particle: object, scale: float) -> tuple[float, float, float, float]:
    value = particle.momentum
    result = tuple(
        float(component) * scale
        for component in (
            value.px,
            value.py,
            value.pz,
            value.e,
        )
    )
    if not all(math.isfinite(component) for component in result):
        raise RuntimeError(f"particle {particle.id} has non-finite momentum")
    return result


def _cross_section(event: object) -> float:
    value = event.cross_section
    if value is None:
        return 0.0
    cross_section = float(value.xsec())
    run_info = event.run_info
    unit_key = "NuHepMC.Units.CrossSection.Unit"
    unit = (
        run_info.attributes[unit_key]
        if run_info is not None and unit_key in run_info.attributes
        else None
    )
    if unit is None:
        return cross_section
    match = re.match(r"^([0-9.eE+-]+)\s*cm2$", _attribute_text(unit))
    if match is None:
        raise RuntimeError(f"unsupported NuHepMC cross-section unit: {unit}")
    return cross_section * float(match.group(1)) / 1.0e-38


def project_event(
    event: object,
    *,
    process_id: int,
    momentum_scale: float,
    vertex_cm: tuple[float, float, float],
    output_event_number: int,
    weight: float | None = None,
) -> RooTrackerEvent:
    incoming = next(
        (
            particle
            for particle in event.particles
            if abs(int(particle.pid)) in NEUTRINO_PDGS and particle.status != 1
        ),
        None,
    )
    target = next(
        (
            particle
            for particle in event.particles
            if 1_000_000_000 <= abs(int(particle.pid)) < 2_000_000_000
        ),
        None,
    )
    nucleon = next(
        (
            particle
            for particle in event.particles
            if abs(int(particle.pid)) in (2112, 2212) and particle.status == 21
        ),
        None,
    )
    final_state = [
        particle
        for particle in event.particles
        if particle.status == 1 and int(particle.pid) not in NUCLEAR_REMNANT_PDGS
    ]
    if not final_state:
        raise RuntimeError(
            f"NuHepMC event {event.event_number} has no transportable final state"
        )

    particles = []
    if incoming is not None:
        particles.append(
            ParticleRecord(int(incoming.pid), 0, _momentum(incoming, momentum_scale))
        )
    if target is not None:
        particles.append(
            ParticleRecord(int(target.pid), 0, _momentum(target, momentum_scale))
        )
    particles.extend(
        ParticleRecord(int(particle.pid), 1, _momentum(particle, momentum_scale))
        for particle in final_state
    )
    if len(particles) > MAX_PARTICLES:
        raise RuntimeError(
            f"NuHepMC event {event.event_number} exceeds RooTracker's "
            f"{MAX_PARTICLES}-particle limit"
        )

    process_name = _process_name(event, process_id)
    current, mode = _reaction_mode(process_name)
    nu_pdg = int(incoming.pid) if incoming is not None else 0
    target_pdg = int(target.pid) if target is not None else -1
    nucleon_pdg = int(nucleon.pid) if nucleon is not None else -1
    reaction = (
        f"nu:{nu_pdg};tgt:{target_pdg};N:{nucleon_pdg};"
        f"proc:{current},{mode};generator_process:{process_id};"
    )
    selected_weight = (
        float(weight)
        if weight is not None
        else float(event.weights[0]) if event.weights else 1.0
    )
    if not math.isfinite(selected_weight):
        raise RuntimeError(f"NuHepMC event {event.event_number} has invalid weight")
    x, y, z = vertex_cm
    return RooTrackerEvent(
        event_number=output_event_number,
        process_id=process_id,
        reaction=reaction,
        vertex_m=(x / 100.0, y / 100.0, z / 100.0, 0.0),
        particles=tuple(particles),
        nucleon_pdg=nucleon_pdg,
        cross_section_1e38_cm2=_cross_section(event),
        weight=selected_weight,
    )


def write_rootracker(events: list[RooTrackerEvent], output_path: Path) -> None:
    """Write the RooTracker schema consumed by edep-sim."""
    try:
        import ROOT  # type: ignore
    except ImportError as error:
        raise RuntimeError("ROOT is required to write RooTracker output") from error

    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_suffix(output_path.suffix + ".tmp")
    root_file = ROOT.TFile.Open(str(temporary), "RECREATE")
    if not root_file or root_file.IsZombie():
        raise RuntimeError(f"could not create RooTracker output: {temporary}")

    tree = ROOT.TTree("gRooTracker", "NuHepMC event tree in RooTracker format")
    event_flags = ROOT.TBits()
    event_code = ROOT.TObjString()
    event_number = array("i", [0])
    event_xsec = array("d", [0.0])
    event_dxsec = array("d", [0.0])
    event_weight = array("d", [1.0])
    event_probability = array("d", [1.0])
    event_vertex = array("d", [0.0] * 4)
    particle_count = array("i", [0])
    pdg = array("i", [0] * MAX_PARTICLES)
    status = array("i", [0] * MAX_PARTICLES)
    rescatter = array("i", [0] * MAX_PARTICLES)
    position = array("d", [0.0] * (MAX_PARTICLES * 4))
    momentum = array("d", [0.0] * (MAX_PARTICLES * 4))
    polarization = array("d", [0.0] * (MAX_PARTICLES * 3))
    first_daughter = array("i", [-1] * MAX_PARTICLES)
    last_daughter = array("i", [-1] * MAX_PARTICLES)
    first_mother = array("i", [-1] * MAX_PARTICLES)
    last_mother = array("i", [-1] * MAX_PARTICLES)
    parent_pdg = array("i", [0])
    parent_decay_mode = array("i", [0])
    parent_decay_momentum = array("d", [0.0] * 4)
    parent_decay_position = array("d", [0.0] * 4)
    parent_production_momentum = array("d", [0.0] * 4)
    parent_production_position = array("d", [0.0] * 4)
    parent_production_vertex = array("i", [0])

    tree.Branch("EvtFlags", "TBits", event_flags, 32000, 1)
    tree.Branch("EvtCode", "TObjString", event_code, 32000, 1)
    tree.Branch("EvtNum", event_number, "EvtNum/I")
    tree.Branch("EvtXSec", event_xsec, "EvtXSec/D")
    tree.Branch("EvtDXSec", event_dxsec, "EvtDXSec/D")
    tree.Branch("EvtWght", event_weight, "EvtWght/D")
    tree.Branch("EvtProb", event_probability, "EvtProb/D")
    tree.Branch("EvtVtx", event_vertex, "EvtVtx[4]/D")
    tree.Branch("StdHepN", particle_count, "StdHepN/I")
    tree.Branch("StdHepPdg", pdg, "StdHepPdg[StdHepN]/I")
    tree.Branch("StdHepStatus", status, "StdHepStatus[StdHepN]/I")
    tree.Branch("StdHepRescat", rescatter, "StdHepRescat[StdHepN]/I")
    tree.Branch("StdHepX4", position, "StdHepX4[StdHepN][4]/D")
    tree.Branch("StdHepP4", momentum, "StdHepP4[StdHepN][4]/D")
    tree.Branch("StdHepPolz", polarization, "StdHepPolz[StdHepN][3]/D")
    tree.Branch("StdHepFd", first_daughter, "StdHepFd[StdHepN]/I")
    tree.Branch("StdHepLd", last_daughter, "StdHepLd[StdHepN]/I")
    tree.Branch("StdHepFm", first_mother, "StdHepFm[StdHepN]/I")
    tree.Branch("StdHepLm", last_mother, "StdHepLm[StdHepN]/I")
    # edep-sim probes these GENIE beam-parent pass-through branches even for
    # generator-neutral RooTracker input.  GiBUU has no dk2nu parent record,
    # so publish explicit zero defaults rather than leaving missing branches.
    tree.Branch("NuParentPdg", parent_pdg, "NuParentPdg/I")
    tree.Branch("NuParentDecMode", parent_decay_mode, "NuParentDecMode/I")
    tree.Branch("NuParentDecP4", parent_decay_momentum, "NuParentDecP4[4]/D")
    tree.Branch("NuParentDecX4", parent_decay_position, "NuParentDecX4[4]/D")
    tree.Branch("NuParentProP4", parent_production_momentum, "NuParentProP4[4]/D")
    tree.Branch("NuParentProX4", parent_production_position, "NuParentProX4[4]/D")
    tree.Branch("NuParentProNVtx", parent_production_vertex, "NuParentProNVtx/I")

    try:
        for event in events:
            event_code.SetString(event.reaction)
            event_number[0] = event.event_number
            event_xsec[0] = event.cross_section_1e38_cm2
            event_dxsec[0] = event.differential_cross_section_1e38_cm2
            event_weight[0] = event.weight
            event_probability[0] = event.probability
            event_vertex[:] = array("d", event.vertex_m)
            particle_count[0] = len(event.particles)
            for index, particle in enumerate(event.particles):
                pdg[index] = particle.pdg
                status[index] = particle.status
                rescatter[index] = 0
                first_daughter[index] = -1
                last_daughter[index] = -1
                first_mother[index] = -1
                last_mother[index] = -1
                for axis, value in enumerate(particle.momentum_gev):
                    momentum[index * 4 + axis] = value
                for axis in range(4):
                    position[index * 4 + axis] = 0.0
                for axis in range(3):
                    polarization[index * 3 + axis] = 0.0
            tree.Fill()
        root_file.cd()
        tree.Write()
        root_file.Write()
    except Exception:
        root_file.Close()
        temporary.unlink(missing_ok=True)
        raise
    else:
        root_file.Close()
    if not temporary.is_file() or temporary.stat().st_size == 0:
        temporary.unlink(missing_ok=True)
        raise RuntimeError(f"RooTracker output was not written: {temporary}")
    temporary.replace(output_path)
