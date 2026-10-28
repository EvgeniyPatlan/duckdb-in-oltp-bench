"""Version lock: ``systems/versions.lock.json`` pins every image and how it was built.

``dbench lock`` records, per system, ``{image, digest, digest_kind, server_version,
duckdb_version, build_inputs}``. For images pulled from a registry the digest is the registry
digest (``digest_kind: "registry"``). Locally built images have no registry digest, so the local
image id is recorded (``"local"``); local ids differ from host to host, so the owner of a bench
host re-runs ``dbench lock`` there after building the images. What must match the committed lock
on every host is ``build_inputs``: base image digests, pinned package versions, tarball SHA-256
values and git tag/commit, all read from the Dockerfiles and build scripts.

``check_lock`` refuses a campaign when the image a system would run differs from the lock
(digest or image id), or when its build inputs differ.
"""
from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable
from pathlib import Path

from dbench import dockerctl
from dbench.systems.registry import DUCKDB_PREFIX, get_adapter

ROOT = Path(__file__).resolve().parents[1]
LOCK_PATH = ROOT / "systems" / "versions.lock.json"
SYSTEMS_DIR = ROOT / "systems"

# Systems whose image is pinned. Track B variants run a Track A image (see lock_name).
LOCKED_SYSTEMS = (
    "duckdb-1.5.5", "duckdb-1.4.3", "duckdb-1.5.3", "duckdb-1.5.2-dev736", "innodb", "postgres",
    "percona", "mariadb", "alisql", "pg_duckdb", "pg_ducklake", "villagesql",
)

# Plain DuckDB versions built from source instead of installed from PyPI: extra build args of
# systems/duckdb/Dockerfile (keep in step with scripts/build-images.sh).
DUCKDB_SOURCE_BUILDS = {
    "1.5.2-dev736": {
        "DUCKDB_SOURCE_COMMIT": "8a66223119b16000f3814fa7150cfa7370b63209",
        "DUCKDB_PYTHON_TAG": "v1.5.2",
        "DUCKDB_GIT_DESCRIBE": "v1.5.1-736-g8a6622311",
    },
}

Identity = tuple[str, tuple[str, ...]]  # ("registry" | "local", digests or (image id,))


class LockError(RuntimeError):
    """The images to run do not match the version lock."""


def lock_name(system: str) -> str:
    """Lock key of a system: Track B variants share the Track A image."""
    return system.removesuffix("-trackb")


def adapter_image(system: str) -> str:
    return get_adapter(lock_name(system), None).image


def _versions(system: str) -> tuple[str, str | None]:
    """(server_version, duckdb_version) as pinned by the benchmark's frozen versions."""
    from dbench.systems import alisql, percona

    if system.startswith(DUCKDB_PREFIX):
        v = system.removeprefix(DUCKDB_PREFIX)
        return f"DuckDB {v}", v
    table = {
        "innodb": ("MySQL 9.7 (InnoDB)", None),
        "postgres": ("PostgreSQL 18", None),
        "percona": ("Percona Server 9.7 + ducksdb-mysql-engine v0.2.3", percona.DUCKDB_VERSION),
        "mariadb": ("MariaDB 13.0.2 + MariaDB-duckdb-engine", "bundled; see versions in records"),
        "alisql": ("AliSQL 8.0.44-3 (MySQL 8.0.44)", alisql.DUCKDB_VERSION),
        "pg_duckdb": ("PostgreSQL 18 + pg_duckdb v1.1.1", "bundled; see versions in records"),
        "pg_ducklake": ("PostgreSQL 18 + pg_ducklake v1.0.3", "bundled; see versions in records"),
        "villagesql": ("VillageSQL 0.0.7 (MySQL 8.4) + vsql_duckdb 0.0.3", "1.5.5"),
    }
    return table[system]


_FROM = re.compile(r"^FROM\s+(\S+)", re.IGNORECASE)
_ARG = re.compile(r"^ARG\s+([A-Za-z_][A-Za-z0-9_]*)=(.*)$")
_SHELL_VAR = re.compile(r'^(TAG|COMMIT)="([^"]*)"')


def _dockerfile_inputs(path: Path) -> dict:
    bases: list[str] = []
    args: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if m := _FROM.match(line):
            bases.append(m.group(1))
        elif m := _ARG.match(line):
            args[m.group(1)] = m.group(2).strip().strip('"')
    return {"from": bases, "args": args}


def build_inputs(system: str) -> dict:
    """Pinned inputs of a system's image; empty for images pulled from a registry."""
    name = lock_name(system)
    if name.startswith(DUCKDB_PREFIX):
        inputs = _dockerfile_inputs(SYSTEMS_DIR / "duckdb" / "Dockerfile")
        version = name.removeprefix(DUCKDB_PREFIX)
        inputs["args"] = {k: v for k, v in inputs["args"].items() if v}
        inputs["args"]["DUCKDB_VERSION"] = version
        source = DUCKDB_SOURCE_BUILDS.get(version)
        if source:
            inputs["args"].update(source)
            inputs["source"] = "github.com/duckdb/duckdb-python at DUCKDB_PYTHON_TAG, " \
                               "DuckDB checked out at DUCKDB_SOURCE_COMMIT"
        else:
            inputs["pip"] = f"duckdb=={version}"
        return inputs
    directory = SYSTEMS_DIR / get_adapter(name, None).config_dir
    if (directory / "Dockerfile").is_file():
        return _dockerfile_inputs(directory / "Dockerfile")
    script = directory / "build.sh"
    if script.is_file():
        found = {}
        for line in script.read_text(encoding="utf-8").splitlines():
            if m := _SHELL_VAR.match(line):
                found[m.group(1)] = m.group(2)
        found["source"] = "github.com/relytcloud/pg_ducklake"
        return found
    return {}


def image_identity(image: str) -> Identity:
    """Registry digests of a pulled image, else its local image id."""
    from docker.errors import ImageNotFound

    try:
        img = dockerctl._client().images.get(image)
    except ImageNotFound:
        raise LockError(f"image {image} is not present; build or pull it first "
                        "(scripts/build-images.sh)") from None
    digests = sorted(d.split("@", 1)[1] for d in img.attrs.get("RepoDigests") or [])
    return ("registry", tuple(digests)) if digests else ("local", (img.id,))


def entry(system: str, identity: Callable[[str], Identity] = image_identity) -> dict:
    image = adapter_image(system)
    kind, values = identity(image)
    server, duck = _versions(lock_name(system))
    return {"image": image, "digest": values[0], "digest_kind": kind, "server_version": server,
            "duckdb_version": duck, "build_inputs": build_inputs(system)}


def _frozen_problems(system: str, old: dict, new: dict) -> list[str]:
    """What a rewrite would change that only ``--force`` may change."""
    problems = []
    if old["image"] != new["image"]:
        problems.append(f"{system}: image changes from {old['image']} to {new['image']}")
    if old["build_inputs"] != new["build_inputs"]:
        problems.append(f"{system}: build inputs differ from the committed lock")
    if old["digest_kind"] == "registry" and (
            new["digest_kind"] != "registry" or old["digest"] != new["digest"]):
        problems.append(f"{system}: registry digest changes from {old['digest']} to "
                        f"{new['digest']} ({new['digest_kind']})")
    return problems


def write_lock(path: Path = LOCK_PATH, systems: Iterable[str] = LOCKED_SYSTEMS,
               identity: Callable[[str], Identity] = image_identity,
               force: bool = False) -> dict:
    """Write the lock. Without ``force`` only local image ids may change relative to the lock
    already at ``path``: registry digests, images and build inputs are frozen."""
    path, systems = Path(path), list(systems)
    old = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    doc = {**old, **{s: entry(s, identity) for s in systems}}
    if not force:
        problems = [p for s in systems if s in old
                    for p in _frozen_problems(s, old[s], doc[s])]
        if problems:
            raise LockError(
                "refusing to change the committed lock:\n  " + "\n  ".join(problems)
                + "\nThe registry digests and build inputs are frozen. Pull the images by the "
                "locked digests (scripts/build-images.sh); pass --force only to move the "
                "pins on purpose.")
    path.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return doc


def _problems(system: str, locked: dict, identity: Callable[[str], Identity],
              inputs_of: Callable[[str], dict]) -> list[str]:
    image = adapter_image(system)
    problems: list[str] = []
    if locked["image"] != image:
        problems.append(f"{system}: image {image} differs from the locked {locked['image']}")
    if inputs_of(system) != locked["build_inputs"]:
        problems.append(
            f"{system}: build inputs differ from the lock (Dockerfile or build script changed: "
            f"now {json.dumps(inputs_of(system), sort_keys=True)}, locked "
            f"{json.dumps(locked['build_inputs'], sort_keys=True)})")
    kind, values = identity(image)
    if kind != locked["digest_kind"] or locked["digest"] not in values:
        what = "image digest" if kind == "registry" else "image id"
        problems.append(
            f"{system}: {what} drift: running image {image} is {', '.join(values)}, "
            f"the lock has {locked['digest']} ({locked['digest_kind']})")
    return problems


def check_lock(
    systems: Iterable[str],
    lock_path: Path = LOCK_PATH,
    identity: Callable[[str], Identity] = image_identity,
    inputs_of: Callable[[str], dict] = build_inputs,
) -> None:
    """Raise LockError unless every system's image and build inputs match the lock."""
    if not Path(lock_path).is_file():
        raise LockError(f"version lock {lock_path} not found; run: dbench lock")
    doc = json.loads(Path(lock_path).read_text(encoding="utf-8"))
    problems: list[str] = []
    for system in sorted({lock_name(s) for s in systems}):
        if system not in doc:
            problems.append(f"{system}: not in the version lock; run: dbench lock")
            continue
        problems.extend(_problems(system, doc[system], identity, inputs_of))
    if problems:
        raise LockError(
            "version lock mismatch, refusing to start:\n  " + "\n  ".join(problems)
            + "\nIf the change is intended, rebuild the images and re-run: dbench lock")
