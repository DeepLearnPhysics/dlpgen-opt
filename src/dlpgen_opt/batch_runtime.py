"""Container launch commands shared by the scheduler renderers."""
import re
import shlex
from pathlib import Path


def container_reference(value, runtime, dry_run):
    if runtime=='shifter':
        value=str(value)
        if not re.fullmatch(r'(?:docker:[A-Za-z0-9][A-Za-z0-9._/:@-]*|id:[a-fA-F0-9]+)',value):
            raise ValueError('Shifter requires a docker:registry/image:tag or id:HASH reference')
        return value
    path=Path(value).expanduser().resolve()
    if not dry_run and not path.is_file():
        raise ValueError(f'Container image does not exist: {path}')
    return path


def container_command(runtime, container, bind_paths, *, cpus=None, shifter_modules='none'):
    if runtime=='shifter':
        parts=['shifter',f'--image={container}',f'--module={shifter_modules}',
               '--clearenv','--env=PYTHONNOUSERSITE=1']
        for bind in bind_paths:
            bind=str(bind)
            parts.extend(['--volume',bind if ':' in bind else f'{bind}:{bind}'])
    else:
        parts=[runtime,'exec','--cleanenv','--env','PYTHONNOUSERSITE=1']
        if bind_paths:
            parts.extend(['--bind',','.join(map(str,bind_paths))])
    command=shlex.join(parts)
    if cpus is not None:
        command+=f' --env "DLPGEN_OPT_CPUS_PER_TASK={cpus}"'
    if runtime!='shifter':
        command+=' '+shlex.quote(str(container))
    return command+' /usr/local/bin/dlpgen-opt-entrypoint'
