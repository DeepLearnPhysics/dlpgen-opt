from __future__ import annotations

import math
from array import array
from pathlib import Path


PARENT_BRANCHES = (
    "NuParentPdg",
    "NuParentDecMode",
    "NuParentDecP4",
    "NuParentDecX4",
    "NuParentProP4",
    "NuParentProX4",
    "NuParentProNVtx",
)


def _leaf(tree, name: str):
    result = tree.GetLeaf(name)
    if result is None:
        raise RuntimeError(f"missing dk2nu leaf in GHEP tree: {name}")
    return result


def add_dk2nu_parent_branches(ghep_path: Path, rootracker_path: Path) -> None:
    """Translate GENIE's dk2nu pass-through record for edep-sim.

    The generic RooTracker format does not preserve dk2nu beam-parent fields,
    while edep-sim's RooTracker reader expects the equivalent legacy branches.
    dk2nu lengths and times are stored in cm and ns; edep-sim expects these
    particular branches in m and s.
    """
    import ROOT  # type: ignore[import-not-found]

    ROOT.PyConfig.IgnoreCommandLineOptions = True
    if ROOT.gSystem.Load("libdk2nuTree") < 0:
        raise RuntimeError("could not load the dk2nu ROOT dictionary")

    source = ROOT.TFile.Open(str(ghep_path), "READ")
    if source is None or source.IsZombie():
        raise RuntimeError(f"could not open GHEP file: {ghep_path}")
    destination = ROOT.TFile.Open(str(rootracker_path), "UPDATE")
    if destination is None or destination.IsZombie():
        source.Close()
        raise RuntimeError(f"could not open RooTracker file: {rootracker_path}")

    try:
        ghep = source.Get("gtree")
        rootracker = destination.Get("gRooTracker")
        if ghep is None or rootracker is None:
            raise RuntimeError("GHEP or RooTracker event tree is missing")
        if ghep.GetBranch("dk2nu") is None:
            raise RuntimeError("GHEP tree does not contain dk2nu pass-through data")
        if ghep.GetEntries() != rootracker.GetEntries():
            raise RuntimeError(
                "GHEP and RooTracker entry counts differ: "
                f"{ghep.GetEntries()} != {rootracker.GetEntries()}"
            )

        existing = [name for name in PARENT_BRANCHES if rootracker.GetBranch(name)]
        if existing:
            if len(existing) == len(PARENT_BRANCHES):
                return
            raise RuntimeError(
                "RooTracker contains only part of the dk2nu parent schema: "
                + ", ".join(existing)
            )

        ghep.SetBranchStatus("*", False)
        for branch_pattern in ("dk2nu", "decay*", "ancestor*"):
            ghep.SetBranchStatus(branch_pattern, True)
        leaves = {
            name: _leaf(ghep, f"dk2nu.{name}")
            for name in (
                "decay.ptype",
                "decay.ndecay",
                "decay.vx",
                "decay.vy",
                "decay.vz",
                "decay.pdpx",
                "decay.pdpy",
                "decay.pdpz",
                "decay.ppdxdz",
                "decay.ppdydz",
                "decay.pppz",
                "decay.ppenergy",
                "ancestor.pdg",
                "ancestor.startx",
                "ancestor.starty",
                "ancestor.startz",
                "ancestor.startt",
            )
        }

        parent_pdg = array("i", [0])
        decay_mode = array("i", [0])
        decay_p4 = array("d", [0.0] * 4)
        decay_x4 = array("d", [0.0] * 4)
        production_p4 = array("d", [0.0] * 4)
        production_x4 = array("d", [0.0] * 4)
        production_vertex = array("i", [0])
        destination.cd()
        branches = (
            rootracker.Branch("NuParentPdg", parent_pdg, "NuParentPdg/I"),
            rootracker.Branch("NuParentDecMode", decay_mode, "NuParentDecMode/I"),
            rootracker.Branch("NuParentDecP4", decay_p4, "NuParentDecP4[4]/D"),
            rootracker.Branch("NuParentDecX4", decay_x4, "NuParentDecX4[4]/D"),
            rootracker.Branch("NuParentProP4", production_p4, "NuParentProP4[4]/D"),
            rootracker.Branch("NuParentProX4", production_x4, "NuParentProX4[4]/D"),
            rootracker.Branch(
                "NuParentProNVtx", production_vertex, "NuParentProNVtx/I"
            ),
        )

        def scalar(name: str) -> float:
            return float(leaves[name].GetValue())

        def assign(buffer: array, values: tuple[float, ...]) -> None:
            for index, value in enumerate(values):
                buffer[index] = value

        particle_db = ROOT.TDatabasePDG.Instance()
        for entry in range(ghep.GetEntries()):
            if ghep.GetEntry(entry) <= 0:
                raise RuntimeError(f"could not read dk2nu GHEP entry {entry}")
            ancestor_count = int(leaves["ancestor.pdg"].GetNdata())
            if ancestor_count < 2:
                raise RuntimeError(
                    f"dk2nu entry {entry} has no neutrino-parent ancestor"
                )
            parent_index = ancestor_count - 2
            neutrino_index = ancestor_count - 1

            parent_pdg[0] = int(scalar("decay.ptype"))
            decay_mode[0] = int(scalar("decay.ndecay"))
            assign(decay_p4, (
                scalar("decay.pdpx"),
                scalar("decay.pdpy"),
                scalar("decay.pdpz"),
                0.0,
            ))
            particle = particle_db.GetParticle(parent_pdg[0])
            mass = float(particle.Mass()) if particle is not None else 0.0
            decay_p4[3] = math.sqrt(
                sum(value * value for value in decay_p4[:3]) + mass * mass
            )
            assign(decay_x4, (
                scalar("decay.vx") / 100.0,
                scalar("decay.vy") / 100.0,
                scalar("decay.vz") / 100.0,
                float(leaves["ancestor.startt"].GetValue(neutrino_index)) * 1.0e-9,
            ))

            parent_pz = scalar("decay.pppz")
            assign(production_p4, (
                scalar("decay.ppdxdz") * parent_pz,
                scalar("decay.ppdydz") * parent_pz,
                parent_pz,
                scalar("decay.ppenergy"),
            ))
            assign(production_x4, (
                float(leaves["ancestor.startx"].GetValue(parent_index)) / 100.0,
                float(leaves["ancestor.starty"].GetValue(parent_index)) / 100.0,
                float(leaves["ancestor.startz"].GetValue(parent_index)) / 100.0,
                float(leaves["ancestor.startt"].GetValue(parent_index)) * 1.0e-9,
            ))
            production_vertex[0] = parent_index
            for branch in branches:
                branch.Fill()

        rootracker.Write("", ROOT.TObject.kOverwrite)
    finally:
        destination.Close()
        source.Close()
