"""Dependency-free PBS profile validation for login-node submission."""
import re

DEFAULTS = dict(
    scheduler='pbs', container_runtime='apptainer', fakeroot=False,
    node_cpus=None, cpu_bind='depth', prepare_jobs_per_task=1,
    cpus_per_task=1, prepare_cpus_per_task=None, jobs_per_task=1,
    time='02:00:00', prepare_time=None, place='scatter:excl', mem=None,
    max_array_size=100, max_concurrent=2, bind_paths=[],
    modules=['apptainer'], module_paths=[])
REQUIRED = ('account', 'queue', 'system', 'filesystems')


def normalize_pbs_profile(settings):
    profile = dict(DEFAULTS, **settings)
    validate_pbs_profile(profile)
    return profile


def validate_pbs_profile(profile):
    unknown = set(profile) - set(DEFAULTS) - set(REQUIRED)
    if unknown:
        raise ValueError('unknown PBS profile fields: ' + ', '.join(sorted(unknown)))
    if any(key not in profile for key in REQUIRED):
        raise ValueError('PBS profiles require account, queue, system and filesystems')
    for key, choices in dict(scheduler=('pbs',), system=('aurora', 'polaris'),
                             container_runtime=('apptainer', 'singularity'),
                             place=('scatter', 'scatter:excl')).items():
        if profile[key] not in choices:
            raise ValueError('invalid PBS ' + key)
    if type(profile['fakeroot']) is not bool:
        raise ValueError('fakeroot must be a boolean')
    for key in ('cpus_per_task', 'jobs_per_task', 'prepare_jobs_per_task',
                'max_array_size', 'max_concurrent', 'node_cpus', 'prepare_cpus_per_task'):
        value = profile[key]
        if value is None and key in ('node_cpus', 'prepare_cpus_per_task'):
            continue
        if type(value) is not int or value <= 0:
            raise ValueError(key + ' must be a positive integer')
    patterns = dict(account=r'[A-Za-z0-9_.:-]+', queue=r'[A-Za-z0-9_.-]+',
                    filesystems=r'[A-Za-z0-9_-]+(?::[A-Za-z0-9_-]+)*',
                    time=r'\d+:\d{2}:\d{2}', prepare_time=r'\d+:\d{2}:\d{2}',
                    mem=r'[0-9]+[KMGTPkmgpt]?[bB]?', cpu_bind=r'(depth|list:[0-9,-]+)')
    for key, pattern in patterns.items():
        value = profile[key]
        if value is None and key in ('prepare_time', 'mem'):
            continue
        if not isinstance(value, str) or not re.fullmatch(pattern, value):
            raise ValueError('invalid PBS ' + key)
    for key in ('modules', 'module_paths', 'bind_paths'):
        if not isinstance(profile[key], list):
            raise ValueError(key + ' must be a list')
    if any(not re.fullmatch(r'[A-Za-z0-9_./+-]+', str(name)) for name in profile['modules']):
        raise ValueError('invalid module name')
    budget = profile['prepare_cpus_per_task'] or profile['cpus_per_task']
    if profile['node_cpus'] is not None:
        if profile['place'] != 'scatter:excl':
            raise ValueError('whole-node profiles require scatter:excl placement')
        if max(budget, profile['cpus_per_task']) > profile['node_cpus']:
            raise ValueError('worker CPU budgets cannot exceed node_cpus')
    if profile['cpu_bind'].startswith('list:'):
        from .cpu_affinity import parse_cpu_set
        ids = parse_cpu_set(profile['cpu_bind'][5:])
        if len(ids) < max(budget, profile['cpus_per_task']):
            raise ValueError('cpu_bind does not expose enough CPUs for worker budgets')
