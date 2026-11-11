"""PBS Professional submission for ALCF Aurora and Polaris CPU workloads."""
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .pbs_runtime import render_script, submit_arrays


class PBSProfile(BaseModel):
    model_config=ConfigDict(extra='forbid')
    scheduler: Literal['pbs']='pbs'
    account: str=Field(pattern=r'^[A-Za-z0-9_.:-]+$')
    queue: str=Field(pattern=r'^[A-Za-z0-9_.-]+$')
    system: Literal['aurora','polaris']
    container_runtime: Literal['apptainer','singularity']='apptainer'
    fakeroot: bool=False
    node_cpus: int | None=Field(default=None,gt=0)
    cpu_bind: str=Field(default='depth',pattern=r'^(depth|list:[0-9,-]+)$')
    prepare_jobs_per_task: int=Field(default=1,gt=0)
    cpus_per_task: int=Field(default=1,gt=0)
    prepare_cpus_per_task: int | None=Field(default=None,gt=0)
    jobs_per_task: int=Field(default=1,gt=0)
    time: str=Field(default='02:00:00',pattern=r'^\d+:\d{2}:\d{2}$')
    prepare_time: str | None=Field(default=None,pattern=r'^\d+:\d{2}:\d{2}$')
    filesystems: str=Field(pattern=r'^[A-Za-z0-9_-]+(?::[A-Za-z0-9_-]+)*$')
    place: Literal['scatter','scatter:excl']='scatter:excl'
    mem: str | None=Field(default=None,pattern=r'^[0-9]+[KMGTPkmgpt]?[bB]?$')
    max_array_size: int=Field(default=100,gt=0)
    max_concurrent: int=Field(default=2,gt=0)
    bind_paths: list[Path]=Field(default_factory=list)
    modules: list[str]=Field(default_factory=lambda:['apptainer'])
    module_paths: list[Path]=Field(default_factory=list)

    @model_validator(mode='after')
    def valid_resources(self):
        from .pbs_profile import validate_pbs_profile
        validate_pbs_profile(self.model_dump())
        return self
