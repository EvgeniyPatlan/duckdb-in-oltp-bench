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

import functools
import hashlib
import json
import re
import subprocess
from collections.abc import Callable, Iterable
from pathlib import Path

from dbench import dockerctl
from dbench.systems.registry import DUCKDB_PREFIX, get_adapter

ROOT = Path(__file__).resolve().parents[1]
LOCK_PATH = ROOT / "systems" / "versions.lock.json"
SYSTEMS_DIR = ROOT / "systems"

# Systems whose image is pinned. Track B variants run a Track A image (see lock_name).
LOCKED_SYSTEMS = (
    "duckdb-2.0.0", "duckdb-1.5.5", "duckdb-1.4.3", "duckdb-1.5.3", "duckdb-1.5.2-dev736",
    "innodb", "postgres",
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
        "percona": ("MySQL 9.7.0 + Percona ducksdb-mysql-engine v0.2.4", percona.DUCKDB_VERSION),
        "mariadb": ("MariaDB 12.3.3 + MariaDB-duckdb-engine", "bundled; see versions in records"),
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


# ---- provenance: which lock a record ran against ------------------------------------------

LOCK_ARCHIVE_DIR = "_lock"  # under <results>/<run_id>/; skipped by iter_records


def lock_sha256(path: Path = LOCK_PATH) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def archived_name(sha256: str) -> str:
    return f"versions.lock.{sha256[:12]}.json"


def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-c", "safe.directory=*", "-C", str(ROOT), *args],
                          capture_output=True, text=True, timeout=30, check=False)


@functools.cache
def _git_state(path: Path) -> tuple[str, bool | None, bool | None]:
    """(HEAD commit, whether the lock file is tracked and equals its committed version, whether
    the harness has uncommitted changes besides the lock); "unknown"/None when git cannot tell
    (no checkout, a worktree whose git dir is not mounted)."""
    path = Path(path).resolve()
    others = [".", f":(exclude){path.relative_to(ROOT)}"] if path.is_relative_to(ROOT) else ["."]
    try:
        head = _git("rev-parse", "HEAD")
        if head.returncode != 0:
            return "unknown", None, None
        tracked = _git("ls-files", "--error-unmatch", "--", str(path)).returncode == 0
        diff = _git("diff", "--quiet", "HEAD", "--", str(path)).returncode
        status = _git("status", "--porcelain", "--untracked-files=no", "--", *others)
    except (OSError, subprocess.SubprocessError):
        return "unknown", None, None
    committed = {0: True, 1: False}.get(diff) if tracked else False
    dirty = bool(status.stdout.strip()) if status.returncode == 0 else None
    return head.stdout.strip(), committed, dirty


@functools.cache
def _lock_bytes(path: Path) -> bytes:
    """The lock file as this process first read it: archive() and provenance() share it, so a
    record's ``sha256`` always names the archived copy. ``check_lock`` refuses a campaign whose
    images differ from the lock, and a later rewrite of the file is not seen by the process."""
    return Path(path).read_bytes()


def _lock_state(path: Path) -> tuple[str, dict]:
    data = _lock_bytes(path)
    return hashlib.sha256(data).hexdigest(), json.loads(data)


def provenance(system: str, path: Path = LOCK_PATH) -> dict:
    """``versions["lock"]`` of a record: the lock file it ran against (its SHA-256 names the copy
    archived with the run, see ``archive``), the git commit of the harness, whether the lock is
    the committed one (False on a bench host that re-ran ``dbench lock``: locally built images
    have per-host ids) and the system's locked image identity."""
    path = Path(path)
    sha, doc = _lock_state(path)
    commit, committed, dirty = _git_state(path)
    out = {"file": "systems/versions.lock.json", "sha256": sha,
           "archived_as": f"{LOCK_ARCHIVE_DIR}/{archived_name(sha)}", "git_commit": commit,
           "lock_is_committed": committed, "harness_dirty": dirty}
    entry = doc.get(lock_name(system))
    if entry:
        out.update(digest=entry["digest"], digest_kind=entry["digest_kind"])
    return out


def archive(run_dir: Path, path: Path = LOCK_PATH) -> Path:
    """Keep a copy of the lock file a run uses next to its records:
    ``<run_dir>/_lock/versions.lock.<sha12>.json``. Idempotent; a run resumed under another lock
    keeps both copies."""
    data = _lock_bytes(Path(path))
    target = Path(run_dir) / LOCK_ARCHIVE_DIR / archived_name(hashlib.sha256(data).hexdigest())
    if not target.is_file():
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".tmp")
        tmp.write_bytes(data)
        tmp.replace(target)
    return target


def _digest_matches(recorded: str, entry: dict) -> bool:
    """``versions.image_digest`` is ``repo@sha256:..`` for a pulled image and the image id for a
    local one; the lock keeps the bare digest or id."""
    return recorded.rsplit("@", 1)[-1] == entry["digest"]


def manifest(run_dir: Path, path: Path = LOCK_PATH) -> dict:
    """Check every record of a run against a lock file and archive that lock with the run.

    For runs whose records predate ``versions.lock``: the manifest
    (``<run_dir>/_lock/manifest.<sha12>.json``) states, per system, how many records name an image
    digest equal to the lock's, which differ, and which name none (the record cannot be
    checked). It never edits a record."""
    from dbench.results import iter_records

    run_dir = Path(run_dir)
    sha, doc = _lock_state(Path(path))
    systems: dict[str, dict] = {}
    for rec in iter_records(run_dir):
        row = systems.setdefault(rec.system, {"match": 0, "differ": [], "no_digest": 0})
        recorded = rec.versions.get("image_digest")
        entry = doc.get(lock_name(rec.system))
        if not recorded or not entry or str(recorded).startswith("unknown"):
            row["no_digest"] += 1
        elif _digest_matches(str(recorded), entry):
            row["match"] += 1
        elif recorded not in row["differ"]:
            row["differ"].append(recorded)
    commit, committed, dirty = _git_state(Path(path))
    out = {"lock_sha256": sha, "archived_as": f"{LOCK_ARCHIVE_DIR}/{archived_name(sha)}",
           "git_commit": commit, "lock_is_committed": committed, "harness_dirty": dirty,
           "systems": systems}
    archive(run_dir, path)
    target = run_dir / LOCK_ARCHIVE_DIR / f"manifest.{sha[:12]}.json"
    target.write_text(json.dumps(out, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return out


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
