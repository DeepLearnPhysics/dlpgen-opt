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
        assert len(scripts) == (6 if suffix == 'sbatch' else 4)
        for script in scripts:
            subprocess.check_call(['bash', '-n', str(script)])
        if site == 'polaris':
            assert all('singularity_fakeroot=true' in p.read_text() and '--fakeroot' in p.read_text()
                       for p in scripts)
    log = (work / 'submissions.txt').read_text()
    assert 'depend=afterok:123[].server' in log
    assert '--dependency afterok:123' in log
print('PASS: Python ' + sys.version.split()[0] + ' Slurm, Polaris and Aurora submission chains')
