"""
This module contains parsing methods for transforming various dict and list schemas into Pipeline, Repository, Task,
and other kinds of pydatatask classes.
"""
from typing import Any, Awaitable, Dict, List, Optional, Type, TypeVar
from collections.abc import Callable, Mapping
from datetime import timedelta
from enum import Enum
from importlib.metadata import entry_points
import base64
import gc
import json
import os
import pathlib
import socket
import sys
import traceback

import aiobotocore.session
import asyncssh
import docker_registry_client_async
import kubernetes_asyncio.config
import motor.motor_asyncio

from pydatatask.executor import Executor
from pydatatask.executor.container_manager import DockerContainerManager
from pydatatask.executor.pod_manager import PodManager
from pydatatask.executor.proc_manager import LocalLinuxManager, SSHLinuxManager
from pydatatask.host import Host, HostOS
from pydatatask.repository import (
    FileRepository,
    Repository,
    S3BucketRepository,
    YamlMetadataFileRepository,
)
from pydatatask.repository.base import (
    DirectoryRepository,
    InProcessBlobRepository,
    InProcessMetadataRepository,
)
from pydatatask.repository.bucket import YamlMetadataS3Repository
from pydatatask.repository.docker import DockerRepository
from pydatatask.repository.mongodb import MongoMetadataRepository
from pydatatask.resource_manager import ResourceManager, Resources
from pydatatask.session import Session
from pydatatask.task import (
    INPUT_KINDS,
    OUTPUT_KINDS,
    ContainerTask,
    KubeTask,
    Link,
    LinkKind,
    ProcessTask,
    Task,
)
import pydatatask

_T = TypeVar("_T")
_K = TypeVar("_K", bound=Task)


def parse_bool(thing: Any) -> bool:
    """
    Parse a string, int, or bool into a bool.
    """
    if isinstance(thing, bool):
        return thing
    if isinstance(thing, int):
        return bool(thing)
    if isinstance(thing, str):
        if thing.lower() in ("yes", "y", "1", "true"):
            return True
        if thing.lower() in ("no", "n", "0", "false"):
            return False
        raise ValueError(f"Invalid bool value {thing}")
    raise ValueError(f"{type(thing)} is not valid as a bool")


_E = TypeVar("_E", bound=Enum)


def make_enum_constructor(cls: Type[_E]) -> Callable[[Any], Optional[_E]]:
    def inner(thing):
        if thing is None:
            return None
        if not isinstance(thing, str):
            raise ValueError(f"{cls} must be instantiated by a string")
        return getattr(cls, thing)

    return inner


def make_constructor(name: str, constructor: Callable[..., _T], schema: Dict[str, Any]) -> Callable[[Any], _T]:
    """
    Generate a constructor function, or a function which will take a dict of parameters, validate them, and call a
    function with them as keywords.
    """
    tdc = make_typeddict_constructor(name, schema)

    def inner(thing):
        return constructor(**tdc(thing))

    return inner


def make_typeddict_constructor(name: str, schema: Dict[str, Any]) -> Callable[[Any], Dict[str, Any]]:
    def inner(thing):
        if not isinstance(thing, dict):
            raise ValueError(f"{name} must be followed by a mapping")

        kwargs = {}
        for k, v in thing.items():
            if k not in schema:
                raise ValueError(f"Invalid argument to {name}: {k}")
            kwargs[k] = schema[k](v)
        return kwargs

    return inner


def make_dispatcher(name: str, mapping: Dict[str, Callable[[Any], _T]]) -> Callable[[Any], _T]:
    """
    Generate a dispatcher function, or a function which accepts a mapping of two keys: cls and args. cls should be one
    keys in the provided mapping, and args are the arguments to the function pulled out of mapping.
    Should be used for situations where you need to pick from one of many implementations of something.
    """

    def inner(thing):
        if not isinstance(thing, dict):
            raise ValueError(f"{name} must be a mapping")
        if "cls" not in thing:
            raise ValueError(f"You must provide the cls name for {name}")
        key = thing["cls"]
        value = thing.get("args", {})
        constructor = mapping.get(key, None)
        if constructor is None:
            raise ValueError(f"{key} is not a valid member of {name}")
        return constructor(value)

    return inner


def make_dict_parser(
    name: str, key_parser: Callable[[str], str], value_parser: Callable[[Any], _T]
) -> Callable[[Any], Dict[str, _T]]:
    """
    Generate a dict parser function, or a function which validates and transforms the keys and values of a dict into
    another dict.
    """

    def inner(thing):
        if not isinstance(thing, dict):
            raise ValueError(f"{name} must be a dict")
        return {key_parser(key): value_parser(value) for key, value in thing.items()}

    return inner


def make_list_parser(name: str, value_parser: Callable[[Any], _T]) -> Callable[[Any], List[_T]]:
    """
    Generate a list parser function, or a function which validates and transforms the members of a list into another
    list.
    """

    def inner(thing):
        if not isinstance(thing, list):
            raise ValueError(f"{name} must be a list")
        return [value_parser(value) for value in thing]

    return inner


def make_picker(name: str, options: Mapping[str, _T]) -> Callable[[Any], Optional[_T]]:
    """
    Generate a picker function, or a function which takes a string and returns one of the members of the provided
    options dict.
    """

    def inner(thing):
        if thing is None:
            return None
        if not options:
            raise ValueError(f"Must provide at least one {name}")
        if not isinstance(thing, str):
            raise ValueError(f"When picking a {name}, must provide a str")
        if thing not in options:
            raise ValueError(f"{thing} is not a valid option for {options}, you want e.g. {next(iter(options))}")
        return options[thing]

    return inner


def _build_s3_connection(endpoint: str, username: str, password: str):
    async def minio():
        minio_session = aiobotocore.session.get_session()
        async with minio_session.create_client(
            "s3",
            endpoint_url=endpoint,
            aws_access_key_id=username,
            aws_secret_access_key=password,
        ) as client:
            yield client

    return minio


def _build_host(name: str, os: HostOS):
    async def host():
        yield Host(name, os)

    return host


def _build_docker_connection(
    domain: str,
    username: Optional[str] = None,
    password: Optional[str] = None,
    config_file: Optional[str] = None,
    default_config_file: bool = False,
):
    if default_config_file:
        config_file = os.path.expanduser("~/.docker/config.json")
    if config_file is not None:
        with open(config_file, "r") as fp:
            docker_config = json.load(fp)
        username, password = base64.b64decode(docker_config["auths"][domain]["auth"]).decode().split(":")
    else:
        if username is None or password is None:
            raise ValueError("Must provide username and password or a config file for DockerRegistry")

    async def docker():
        registry = docker_registry_client_async.DockerRegistryClientAsync(
            client_session_kwargs={"connector_owner": True},
            tcp_connector_kwargs={"family": socket.AF_INET},
            ssl=True,
        )
        await registry.add_credentials(
            credentials=base64.b64encode(f"{username}:{password}".encode()).decode(),
            endpoint=domain,
        )
        yield registry
        await registry.close()
        gc.collect()

    return docker


def _build_mongo_connection(url: str, database: str):
    async def mongo():
        client = motor.motor_asyncio.AsyncIOMotorClient(url)
        collection = client.get_database(database)
        yield collection

    return mongo


def _build_ssh_connection(
    hostname: str, username: str, password: Optional[str] = None, key: Optional[str] = None, port: int = 22
):
    async def ssh():
        async with asyncssh.connect(
            hostname,
            port=port,
            username=username,
            password=password,
            known_hosts=None,
            client_keys=asyncssh.load_keypairs(key) if key is not None else None,
        ) as s:
            yield s

    return ssh


_quota_constructor = make_constructor("quota", Resources.parse, {"cpu": str, "mem": str, "launches": str})
_timedelta_constructor = make_constructor(
    "timedelta",
    timedelta,
    {"days": int, "seconds": int, "microseconds": int, "milliseconds": int, "minutes": int, "hours": int, "weeks": int},
)


def build_repository_picker(resources: Dict[str, Callable[[], Any]]) -> Callable[[Any], Repository]:
    kinds: Dict[str, Callable[[Any], Repository]] = {
        "InProcessMetadata": make_constructor(
            "InProcessMetadataRepository",
            InProcessMetadataRepository,
            {},
        ),
        "InProcessBlob": make_constructor(
            "InProcessBlobRepository",
            InProcessBlobRepository,
            {},
        ),
        "File": make_constructor(
            "FileRepository",
            FileRepository,
            {
                "basedir": str,
                "extension": str,
                "case_insensitive": parse_bool,
            },
        ),
        "Directory": make_constructor(
            "DirectoryRepository",
            DirectoryRepository,
            {
                "basedir": str,
                "extension": str,
                "case_insensitive": parse_bool,
                "discard_empty": parse_bool,
            },
        ),
        "YamlFile": make_constructor(
            "YamlMetadataFileRepository",
            YamlMetadataFileRepository,
            {
                "basedir": str,
                "extension": str,
                "case_insensitive": parse_bool,
            },
        ),
        "S3Bucket": make_constructor(
            "S3BucketRepository",
            S3BucketRepository,
            {
                "client": make_picker("S3Connection", resources),
                "bucket": str,
                "prefix": str,
                "suffix": str,
                "mimetype": str,
                "incluster_endpoint": str,
            },
        ),
        "YamlMetadataS3Bucket": make_constructor(
            "YamlMetadataS3Repository",
            YamlMetadataS3Repository,
            {
                "client": make_picker("S3Connection", resources),
                "bucket": str,
                "prefix": str,
                "suffix": str,
                "mimetype": str,
                "incluster_endpoint": str,
            },
        ),
        "DockerRegistry": make_constructor(
            "DockerRepository",
            DockerRepository,
            {
                "registry": make_picker("DockerRegistry", resources),
                "domain": str,
                "repository": str,
            },
        ),
        "MongoMetadata": make_constructor(
            "MongoMetadataRepository",
            MongoMetadataRepository,
            {
                "database": make_picker("MongoDatabase", resources),
                "collection": str,
            },
        ),
    }
    for ep in entry_points(group="pydatatask.repository_constructors"):
        maker = ep.load()
        try:
            kinds |= maker(resources)
        except TypeError:
            traceback.print_exc(file=sys.stderr)
    return make_dispatcher("Repository", kinds)


def build_executor_picker(session: Session, resources: Dict[str, Callable[[], Any]]) -> Callable[[Any], Executor]:
    def _build_pod_manager(host: Host, app: str, namespace: str, config_file: Optional[str], context: Optional[str]):
        @session.resource
        async def config():
            yield await kubernetes_asyncio.config.load_kube_config(config_file, context)

        return PodManager(host, app, namespace, config)

    kinds: Dict[str, Callable[[Any], Executor]] = {
        "LocalLinux": make_constructor(
            "LocalLinuxManager",
            LocalLinuxManager,
            {
                "app": str,
                "local_path": str,
            },
        ),
        "SSHLinux": make_constructor(
            "SSHLinuxManager",
            SSHLinuxManager,
            {
                "host": make_picker("Host", resources),
                "app": str,
                "remote_path": str,
                "ssh": make_picker("SSHConnection", resources),
            },
        ),
        "Kubernetes": make_constructor(
            "PodManager",
            _build_pod_manager,
            {
                "host": make_picker("Host", resources),
                "app": str,
                "namespace": str,
                "config_file": str,
                "context": str,
            },
        ),
        "Docker": make_constructor(
            "DockerContainerManager",
            DockerContainerManager,
            {
                "host": make_picker("Host", resources),
                "app": str,
                "url": str,
            },
        ),
    }
    for ep in entry_points(group="pydatatask.executor_constructors"):
        maker = ep.load()
        try:
            kinds |= maker(resources)
        except TypeError:
            traceback.print_exc(file=sys.stderr)
    return make_dispatcher("Executor", kinds)


def build_resource_picker() -> Callable[[Any], Callable[[], Any]]:
    kinds = {
        "Host": make_constructor(
            "Host",
            _build_host,
            {
                "name": str,
                "os": make_enum_constructor(HostOS),
            },
        ),
        "S3Connection": make_constructor(
            "S3Connection",
            _build_s3_connection,
            {
                "endpoint": str,
                "username": str,
                "password": str,
            },
        ),
        "DockerRegistry": make_constructor(
            "DockerRegistry",
            _build_docker_connection,
            {
                "domain": str,
                "username": str,
                "password": str,
                "config_file": str,
                "default_config_file": parse_bool,
            },
        ),
        "MongoDatabase": make_constructor(
            "MongoDatabase",
            _build_mongo_connection,
            {
                "url": str,
                "database": str,
            },
        ),
        "SSHConnection": make_constructor(
            "SSHConnection",
            _build_ssh_connection,
            {
                "hostname": str,
                "username": str,
                "password": str,
                "key": str,
                "port": int,
            },
        ),
    }
    for ep in entry_points(group="pydatatask.resource_constructors"):
        maker = ep.load()
        try:
            kinds |= maker()
        except TypeError:
            traceback.print_exc(file=sys.stderr)
    return make_dispatcher("Resource", kinds)


link_kind_constructor = make_enum_constructor(LinkKind)


def build_task_picker(
    repos: Dict[str, Repository],
    executors: Dict[str, Executor],
    quotas: Dict[str, ResourceManager],
    resources: Dict[str, Callable[[], Any]],
) -> Callable[[str, Any], Task]:
    link_constructor = make_typeddict_constructor(
        "Link",
        {
            "repo": make_picker("Repository", repos),
            "kind": link_kind_constructor,
            "key": lambda thing: None if thing is None else str(thing),
            "is_input": parse_bool,
            "is_output": parse_bool,
            "is_status": parse_bool,
            "inhibits_start": parse_bool,
            "required_for_start": parse_bool,
            "inhibits_output": parse_bool,
            "required_for_output": parse_bool,
        },
    )
    links_constructor = make_dict_parser("links", str, link_constructor)
    kinds = {
        "Process": make_constructor(
            "ProcessTask",
            ProcessTask,
            {
                "name": str,
                "template": str,
                "executor": make_picker("Executor", executors),
                "resource_manager": make_picker("ResourceManager", quotas),
                "job_resources": _quota_constructor,
                "pids": make_picker("Repository", repos),
                "window": _timedelta_constructor,
                "environ": make_dict_parser("environ", str, str),
                "done": make_picker("Repository", repos),
                "stdin": make_picker("Repository", repos),
                "stdout": make_picker("Repository", repos),
                "stderr": lambda thing: pydatatask.task.STDOUT
                if thing == "STDOUT"
                else make_picker("Repository", repos)(thing),
                "ready": make_picker("Repository", repos),
                "links": links_constructor,
            },
        ),
        "Kubernetes": make_constructor(
            "KubeTask",
            KubeTask,
            {
                "name": str,
                "executor": make_picker("Executor", executors),
                "resources": make_picker("ResourceManager", quotas),
                "template": str,
                "logs": make_picker("Repository", repos),
                "done": make_picker("Repository", repos),
                "window": _timedelta_constructor,
                "timeout": _timedelta_constructor,
                "env": make_dict_parser("environ", str, str),
                "ready": make_picker("Repository", repos),
                "links": links_constructor,
            },
        ),
        "Container": make_constructor(
            "ContainerTask",
            ContainerTask,
            {
                "name": str,
                "image": str,
                "template": str,
                "executor": make_picker("Executor", executors),
                "entrypoint": make_list_parser("entrypoint", str),
                "resource_manager": make_picker("ResourceManager", quotas),
                "job_resources": _quota_constructor,
                "window": _timedelta_constructor,
                "environ": make_dict_parser("environ", str, str),
                "logs": make_picker("Repository", repos),
                "done": make_picker("Repository", repos),
                "ready": make_picker("Repository", repos),
                "links": links_constructor,
            },
        ),
    }
    for ep in entry_points(group="pydatatask.task_constructors"):
        maker = ep.load()
        try:
            kinds |= maker(repos, quotas, resources)
        except TypeError:
            traceback.print_exc(file=sys.stderr)
    dispatcher = make_dispatcher("Task", kinds)

    def constructor(name, thing):
        executable = thing.pop("executable")
        executable["args"] |= thing
        executable["args"]["name"] = name
        links = links_constructor(executable["args"].pop("links", {}) or {})
        task = dispatcher(executable)
        for name, link in links.items():
            if link["kind"] in INPUT_KINDS:
                link["is_input"] = True
            if link["kind"] in OUTPUT_KINDS:
                link["is_output"] = True
            task.link(name, **link)
        return task

    return constructor


def find_config() -> Optional[pathlib.Path]:
    thing = os.getenv("PIPELINE_YAML")
    if thing is not None:
        return pathlib.Path(thing)

    root = pathlib.Path.cwd()
    while True:
        pth = root / "pipeline.yaml"
        if pth.exists():
            return pth
        newroot = root.parent
        if newroot == root:
            return None
        else:
            root = newroot