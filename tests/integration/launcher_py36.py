"""Run with Python 3.6 + PyYAML: exercise login-node launchers with fake schedulers."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import yaml

root = Path(__file__).resolve().parents[2]
with tempfile.TemporaryDirectory() as directory:
    work = Path(directory)
    image = work / 'image.sif'
    image.touch()
    for scheduler in ('qsub', 'sbatch'):
        executable = work / scheduler
        executable.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$SUBMISSION_LOG"\n'
                              + ('echo "123[].server"\n' if scheduler == 'qsub' else 'echo "123;cluster"\n'))
        executable.chmod(0o755)
    env = dict(os.environ, PATH=str(work) + ':' + os.environ['PATH'],
               SUBMISSION_LOG=str(work / 'submissions.txt'))
    for site in ('s3df_milano', 'polaris', 'aurora'):
        raw = yaml.safe_load((root / 'configs/production.gibuu-bnb.yaml').read_text())
        raw['production']['jobs'] = 125
        raw['production']['output_dir'] = str(work / site)
        raw['source']['config'] = str(root / 'configs/gibuu/bnb_sbnd.yaml')
        config = work / (site + '.yaml')
        config.write_text(yaml.safe_dump(raw))
        command = [sys.executable, str(root / 'submit.py'), str(config), '--profile', site,
                   '--container', str(image), '--max-concurrent', '1']
        if site.startswith('s3df'):
            command += ['--runtime', 'apptainer']
        dry = subprocess.check_output(command + ['--dry-run'], env=env, universal_newlines=True)
        assert '--phase plan' in dry and '--phase finalize' in dry
        assert not (work / site).exists()
        subprocess.check_call(command, env=env)
        suffix = 'sbatch' if site.startswith('s3df') else 'pbs'
        scripts = list((work / site).rglob('*.' + suffix))
        assert len(scripts) == (6 if suffix == 'sbatch' else 1)
        for script in scripts:
            subprocess.check_call(['bash', '-n', str(script)])
        if site == 'aurora':
            assert all('#PBS -l select=1:mpiprocs=1\n' in p.read_text()
                       and ':system=' not in p.read_text() for p in scripts)
        if site == 'polaris':
            assert all('#PBS -l select=1:system=polaris:mpiprocs=1\n' in p.read_text() for p in scripts)
            assert all('singularity_fakeroot=true' in p.read_text() and '--fakeroot' in p.read_text()
                       for p in scripts)
    production = work / 'merge_input'
    production.mkdir()
    (production / 'manifest.yaml').write_text('production: merge_test\n')
    (production / 'resolved_config.yaml').write_text('production:\n  jobs: 2\n')
    for index in range(2):
        job = production / 'jobs' / ('%05d' % index)
        (job / 'supera').mkdir(parents=True)
        (job / 'supera.yaml').write_text('status: completed\n')
        (job / 'supera/supera.root').write_bytes(b'x')
    for site in ('aurora', 'polaris', 'nersc_cpu'):
        output = work / ('merge_' + site)
        command = [sys.executable, str(root / 'merge.py'), str(production),
                   '--profile', site, '--container',
                   'docker:ghcr.io/deeplearnphysics/dlpgen-opt:0.5.1' if site == 'nersc_cpu' else str(image),
                   '--output-dir', str(output)]
        subprocess.check_call(command, env=env)
        scripts = list(output.rglob('*.sbatch' if site == 'nersc_cpu' else '*.pbs'))
        assert len(scripts) == 1
        subprocess.check_call(['bash', '-n', str(scripts[0])])
        assert 'hadd -j ' in scripts[0].read_text()
    log = (work / 'submissions.txt').read_text()
    assert 'depend=afterok:123[].server' not in log
    assert '--dependency afterok:123' in log
print('PASS: Python ' + sys.version.split()[0] + ' Slurm, Polaris and Aurora submission chains')
