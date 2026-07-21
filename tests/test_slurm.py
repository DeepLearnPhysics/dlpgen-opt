from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

from dlpgen_opt.slurm import SlurmProfile, submit_arrays


def profile(max_array_size: int = 99) -> SlurmProfile:
    return SlurmProfile(
        account="neutrino:ml-dev",
        partition="milano",
        cpus_per_task=1,
        mem_per_cpu="4G",
        time="02:00:00",
        max_array_size=max_array_size,
        bind_paths=[Path("/sdf")],
    )


def test_dry_run_renders_zero_based_array(production_config, tmp_path, capsys):
    container = tmp_path / "image.sif"
    submit_arrays(
        production_config,
        profile(),
        container=container,
        max_concurrent=1,
        dry_run=True,
    )
    script = capsys.readouterr().out
    assert "#SBATCH --partition=milano" in script
    assert "#SBATCH --array=0-1%1" in script
    assert (
        "singularity exec --cleanenv --env PYTHONNOUSERSITE=1 --bind /sdf"
        in script
    )
    assert "--job \"${SLURM_ARRAY_TASK_ID}\"" in script
    assert (
        "dlpgen-opt job ${SLURM_JOB_ID}/${SLURM_ARRAY_TASK_ID} "
        "completed successfully"
    ) in script
    assert 'echo "Completed: $(date --iso-8601=seconds)"' in script
    assert not production_config.production.output_dir.exists()


def test_submit_chunks_at_profile_limit(production_config, tmp_path):
    production_config.production.jobs = 3
    container = tmp_path / "image.sif"
    container.touch()
    completed = subprocess.CompletedProcess(
        args=["sbatch"], returncode=0, stdout="12345;cluster\n", stderr=""
    )
    with patch("dlpgen_opt.slurm.subprocess.run", return_value=completed) as run:
        results = submit_arrays(
            production_config,
            profile(max_array_size=2),
            container=container,
        )

    assert [job_id for _, job_id in results] == ["12345", "12345"]
    assert run.call_count == 2
    assert run.call_args_list[1].args[0][2:4] == ["--dependency", "afterok:12345"]
    scripts = [path.read_text(encoding="utf-8") for path, _ in results]
    assert "#SBATCH --array=0-1" in scripts[0]
    assert "#SBATCH --array=2-2" in scripts[1]
