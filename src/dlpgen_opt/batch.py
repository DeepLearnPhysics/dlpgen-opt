"""Select a batch backend from a strict site profile."""
from pathlib import Path
import yaml

from .slurm import SlurmProfile
from .pbs import PBSProfile


def load_profile(path,name):
    path=Path(path).expanduser().resolve()
    raw=yaml.safe_load(path.read_text())
    if not isinstance(raw,dict) or not isinstance(raw.get('profiles'),dict):
        raise ValueError(f'Batch profile file must contain a profiles mapping: {path}')
    if name not in raw['profiles']:
        raise ValueError(f'unknown batch profile {name!r}; available profiles: {", ".join(sorted(raw["profiles"]))}')
    settings=raw['profiles'][name]
    if not isinstance(settings,dict):
        raise ValueError('batch profile must be a mapping')
    scheduler=settings.get('scheduler','slurm')
    if scheduler not in ('slurm','pbs'):
        raise ValueError(f'unsupported scheduler: {scheduler}')
    return (PBSProfile if scheduler=='pbs' else SlurmProfile).model_validate(settings)


def submit_arrays(config,profile,**kwargs):
    if profile.account=='REPLACE_WITH_PROJECT' and not kwargs.get('dry_run',False):
        raise ValueError('Replace REPLACE_WITH_PROJECT in the site profile with your allocation')
    if isinstance(profile,PBSProfile):
        from .pbs import submit_arrays as submit
    else:
        from .slurm import submit_arrays as submit
    return submit(config,profile,**kwargs)
