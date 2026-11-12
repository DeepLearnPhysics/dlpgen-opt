"""Discover host mounts without importing the production/physics models (Python 3.6)."""
from pathlib import Path
import glob
import yaml


def production_mounts(config, profile_paths=(), extra_paths=()):
    """Mount configs, output storage and host-visible paths referenced by YAML.

    Container-only paths such as /opt/gibuu are left to the image. Explicit
    source:destination:mode mounts are preserved and appended after auto mounts.
    """
    mounts = []
    visited = set()

    def directory(path):
        path = Path(path).expanduser().resolve()
        while not path.is_dir() and path != path.parent:
            path = path.parent
        if any(path == Path(mount) or Path(mount) in path.parents for mount in mounts):
            return
        if path != path.parent and str(path) not in mounts:
            mounts.append(str(path))

    def scan(path):
        path = Path(path).resolve()
        if path in visited:
            return
        visited.add(path)
        directory(path.parent)
        with path.open(encoding='utf-8') as stream:
            raw = yaml.safe_load(stream)

        def walk(value):
            if isinstance(value, dict):
                for key, item in value.items():
                    if key not in ('output_dir', 'container_image') and not str(key).endswith('executable'):
                        walk(item)
            elif isinstance(value, list):
                for item in value:
                    walk(item)
            elif isinstance(value, str) and (('/' in value or value.endswith(('.yaml', '.yml', '.gdml', '.root'))) and '\n' not in value):
                candidate = Path(value).expanduser()
                if not candidate.is_absolute():
                    candidate = path.parent / candidate
                # Do not enumerate a potentially enormous flux catalog.
                prefix = str(candidate)
                if glob.has_magic(prefix):
                    prefix = prefix[:min(i for i, char in enumerate(prefix) if char in '*?[')]
                    candidate = Path(prefix.rsplit('/', 1)[0])
                if candidate.exists():
                    directory(candidate if candidate.is_dir() else candidate.parent)
                    if candidate.is_file() and candidate.suffix in ('.yaml', '.yml'):
                        scan(candidate)
        walk(raw)

    for path in profile_paths:
        mounts.append(str(Path(path).expanduser().resolve()))
    directory(Path(config.config_path).parent)
    # Shared flux/GiBUU caches are siblings of the production directory.
    directory(Path(config.production.output_dir).parent)
    scan(config.config_path)
    for path in extra_paths:
        parts = str(path).split(':', 1)
        value = str(Path(parts[0]).expanduser().resolve())
        if len(parts) == 2:
            value += ':' + parts[1]
        mounts.append(value)
    return list(dict.fromkeys(mounts))
