"""Campaign runner: one YAML file runs every system and measure of a benchmark pass.

Schema (all keys but ``timeout_s``, ``reference`` and ``track_b`` are required)::

    run_id: gate-sf10
    sf: 10
    systems: [duckdb-1.5.5, innodb, postgres, alisql, ...]
    measures: [M1]                      # must be registered in dbench.measures.registry
    limits: {db_cpuset: "0-15", runner_cpuset: "16-23", mem_gb: 64}
    timeout_s: 900
    reference: {system: duckdb-1.5.5, cross_check: [duckdb-1.4.3]}
    track_b: {enabled: true, systems: [duckdb-1.5.5, villagesql, ...]}   # or just ``true``

Before any container starts the limits are validated against the CPUs and RAM the Docker daemon
reports, and every image is checked against ``systems/versions.lock.json``. Systems then run one
at a time: start, load (bulk path, timed as M2 ``load_bulk`` when that cell is missing), M3
storage before any query, the query measures, then M2 ``load_convert`` on a fresh datadir; stop
and delete the data. A cell whose result file
exists is skipped (resume), except automatic failures, which are retried. A measure the system
does not support is written as ``n/a`` with the reason. A system that cannot start, load or finish
is written as ``failed`` with ``extra.auto`` for its pending cells; ``mark-failed`` replaces those
with a final failure (reason and repro) once the time-box is over.

Correctness gate. At SF10 and below the reference is plain DuckDB 1.5.5 and InnoDB and Postgres
must agree with it on every ``ok`` query; at SF100 and above the reference is cross-checked
against the ``cross_check`` systems. A disagreement stops the campaign (``CampaignBlocked``) and
is recorded in ``extra.reference_disagreement``. Engines under test that disagree are findings
(``wrong_result``), not blocking.

Track B runs after Track A under the run id ``<run_id>-trackb``: Track B and Track A both use
plain DuckDB 1.5.5 and would otherwise write the same result files.
"""
from __future__ import annotations

import dataclasses
import os
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import yaml

from dbench import dockerctl, hostinfo, lock
from dbench.config import QUERY_TIMEOUT_S, Limits
from dbench.correctness import compare
from dbench.datagen import DatasetPaths
from dbench.measures import registry as measures
from dbench.measures.load import run_load_bulk
from dbench.measures.power import SUSPEND_REASON, HostSuspended
from dbench.reference import build_reference, cached_reference
from dbench.results import ResultRecord, cell_record, is_done, iter_records, write
from dbench.systems.base import SystemAdapter
from dbench.systems.registry import DUCKDB_PREFIX, get_adapter, known_systems

REFERENCE_SYSTEM = "duckdb-1.5.5"
GATE_SYSTEMS = ("innodb", "postgres")
GATE_MAX_SF = 10  # up to here InnoDB and Postgres gate the run; above, cross-check
QUERIES = range(1, 23)
TRACK_B_JOIN_ITEM = "join_innodb"
KEYS = {"run_id", "sf", "systems", "measures", "limits", "timeout_s", "reference", "track_b"}
LIMIT_KEYS = ("db_cpuset", "runner_cpuset", "mem_gb")


class CampaignError(Exception):
    """The campaign file or its environment is unusable."""


class CampaignBlocked(CampaignError):
    """A correctness disagreement that must be resolved before the campaign can go on."""


@dataclass(frozen=True)
class Campaign:
    run_id: str
    sf: int
    systems: tuple[str, ...]
    measures: tuple[str, ...]
    limits: Limits
    timeout_s: int = QUERY_TIMEOUT_S
    reference_system: str = REFERENCE_SYSTEM
    cross_check: tuple[str, ...] = ()
    track_b_enabled: bool = False
    track_b_systems: tuple[str, ...] = ()

    @property
    def trackb_run_id(self) -> str:
        return f"{self.run_id}-trackb"

    @property
    def gated(self) -> bool:
        return self.sf <= GATE_MAX_SF

    def ordered_systems(self) -> list[str]:
        """Listed order; at SF10 and below the gate systems go first so a wrong result stops
        the campaign before the engines run."""
        if not self.gated:
            return list(self.systems)
        gate = [s for s in GATE_SYSTEMS if s in self.systems]
        return gate + [s for s in self.systems if s not in gate]

    def lock_systems(self) -> set[str]:
        names = set(self.systems) | {self.reference_system, *self.cross_check_systems()}
        if self.track_b_enabled:
            names |= set(self.track_b_systems)
        return names

    def cross_check_systems(self) -> tuple[str, ...]:
        return () if self.gated else self.cross_check

    def planned(self, system: str) -> list[tuple[str, str, str, str]]:
        """Planned cells of ``system`` as (run_id, measure, item, track label or "")."""
        cells = []
        if system in self.systems:
            for m in self.measures:
                cells += [(self.run_id, m, item, "") for item in measures.get(m).items(self.sf)]
        if self.track_b_enabled and system in self.track_b_systems:
            cells += [(self.trackb_run_id, "M1", item, "B") for item in trackb_items()]
        return cells


@dataclass
class Summary:
    written: int = 0
    skipped: int = 0


def trackb_items() -> list[str]:
    return [f"q{n:02d}" for n in QUERIES] + [TRACK_B_JOIN_ITEM]


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _default_track_b(*args, **kwargs):
    from dbench.measures.villagesql import run_track_b

    return run_track_b(*args, **kwargs)


def verified_dataset(sf: float) -> DatasetPaths:
    from dbench import datagen

    root = datagen.default_out() / datagen.dataset_dir_name(sf)
    paths = datagen.load_paths(root) if (root / datagen.MANIFEST).is_file() else None
    if paths is None or not datagen.verify(paths):
        raise CampaignError(
            f"dataset missing or corrupt at {root}; run: dbench datagen --sf {sf}")
    return paths


@dataclass
class Deps:
    """Everything the runner touches outside this process; tests substitute fakes."""

    adapter_factory: Callable[[str, Path | None], SystemAdapter] = get_adapter
    host_resources: Callable[[], tuple[int, int]] = dockerctl.host_resources
    affinity: Callable[[], Iterable[int]] = lambda: os.sched_getaffinity(0)
    lock_check: Callable[[Iterable[str]], None] = lock.check_lock
    dataset: Callable[[float], DatasetPaths] = verified_dataset
    run_track_b: Callable = _default_track_b
    drop_caches: Callable[[], None] = dockerctl.drop_caches
    host: Callable[[], dict] = hostinfo.host_info
    clock: Callable[[], datetime] = _utcnow
    out: Callable[[str], None] = print


# ---- loading and validation -------------------------------------------------------------

def _need(doc: dict, key: str):
    if key not in doc:
        raise CampaignError(f"campaign is missing {key!r}")
    return doc[key]


def _system_list(value, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value or not all(isinstance(s, str) for s in value):
        raise CampaignError(f"{label} must be a non-empty list of system names")
    known = set(known_systems())
    for s in value:
        if s.startswith(DUCKDB_PREFIX) and len(s) > len(DUCKDB_PREFIX):
            continue
        if s not in known:
            raise CampaignError(f"unknown system {s!r} in {label}; expected one of {sorted(known)}")
    return tuple(value)


def _positive_int(value, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise CampaignError(f"{label} must be a positive integer, got {value!r}")
    return value


def _limits(value) -> Limits:
    if not isinstance(value, dict) or set(value) != set(LIMIT_KEYS):
        raise CampaignError(f"limits must have exactly the keys {list(LIMIT_KEYS)}")
    for k in ("db_cpuset", "runner_cpuset"):
        if not isinstance(value[k], str) or not value[k].strip():
            raise CampaignError(f"limits.{k} must be a cpuset string such as \"0-15\"")
    return Limits(value["db_cpuset"], value["runner_cpuset"],
                  _positive_int(value["mem_gb"], "limits.mem_gb"))


def _measures(value) -> tuple[str, ...]:
    if not isinstance(value, list) or not value or not all(isinstance(m, str) for m in value):
        raise CampaignError("measures must be a non-empty list of measure ids")
    missing = [m for m in value if m not in measures.registered()]
    if missing:
        raise CampaignError(
            f"measure(s) {missing} are not registered; registered: {measures.registered()}")
    return tuple(value)


def _track_b(value) -> tuple[bool, tuple[str, ...]]:
    from dbench.measures.villagesql import TRACK_B_SYSTEMS

    if isinstance(value, bool):
        return value, tuple(TRACK_B_SYSTEMS)
    if not isinstance(value, dict) or set(value) - {"enabled", "systems"}:
        raise CampaignError("track_b must be true/false or {enabled: bool, systems: [...]}")
    enabled = value.get("enabled", True)
    if not isinstance(enabled, bool):
        raise CampaignError("track_b.enabled must be true or false")
    systems = _system_list(value["systems"], "track_b.systems") \
        if "systems" in value else tuple(TRACK_B_SYSTEMS)
    return enabled, systems


def load_campaign(path: Path) -> Campaign:
    try:
        doc = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise CampaignError(f"cannot read campaign {path}: {exc}") from exc
    if not isinstance(doc, dict):
        raise CampaignError(f"campaign {path} must be a YAML mapping")
    unknown = sorted(set(doc) - KEYS)
    if unknown:
        raise CampaignError(f"unknown campaign key(s) {unknown}; allowed: {sorted(KEYS)}")
    run_id = _need(doc, "run_id")
    if not isinstance(run_id, str) or not run_id.strip():
        raise CampaignError("run_id must be a non-empty string")
    sf = _positive_int(_need(doc, "sf"), "sf")
    systems = _system_list(_need(doc, "systems"), "systems")
    ref = doc.get("reference") or {}
    if not isinstance(ref, dict) or set(ref) - {"system", "cross_check"}:
        raise CampaignError("reference must be {system: ..., cross_check: [...]}")
    ref_system = _system_list([ref.get("system", REFERENCE_SYSTEM)], "reference.system")[0]
    cross = _system_list(ref["cross_check"], "reference.cross_check") \
        if ref.get("cross_check") else ("duckdb-1.4.3",)
    enabled, trackb_systems = _track_b(doc.get("track_b", False))
    campaign = Campaign(
        run_id=run_id, sf=sf, systems=systems, measures=_measures(_need(doc, "measures")),
        limits=_limits(_need(doc, "limits")),
        timeout_s=_positive_int(doc.get("timeout_s", QUERY_TIMEOUT_S), "timeout_s"),
        reference_system=ref_system, cross_check=cross, track_b_enabled=enabled,
        track_b_systems=trackb_systems)
    missing = [s for s in GATE_SYSTEMS if s not in systems]
    if campaign.gated and missing:
        raise CampaignError(
            f"at SF{sf} the InnoDB and Postgres results gate the run; systems must include "
            f"{missing}")
    return campaign


# ---- records ---------------------------------------------------------------------------

def _record(adapter: SystemAdapter, c: Campaign, deps: Deps, host: dict, *, run_id: str,
            measure: str, item: str, status: str, track: str = "", reason: str | None = None,
            error: str | None = None, extra: dict | None = None) -> ResultRecord:
    unit = measures.get(measure).unit if measure in measures.registered() else "s"
    return ResultRecord(
        system=adapter.name, track=track or adapter.track, measure=measure, sf=c.sf, item=item,
        status=status, value=None, unit=unit, config_ref=f"systems/{adapter.config_dir}/",
        run_id=run_id, started_at=deps.clock().isoformat(), extra=extra or {}, error=error,
        reason=reason, host=host)


def mark_failed(
    campaign_path: Path,
    root: Path,
    *,
    system: str,
    error: str,
    repro: str,
    run_id: str | None = None,
    host: Callable[[], dict] = hostinfo.host_info,
    clock: Callable[[], datetime] = _utcnow,
    adapter_factory: Callable[[str, Path | None], SystemAdapter] = get_adapter,
) -> int:
    """Write a ``failed`` record (reason = ``error``, extra.repro = ``repro``) for every cell
    ``system`` has planned in the campaign that has no record or only an automatic failure;
    real measurements are never overwritten. Returns how many cells were written."""
    c = load_campaign(campaign_path)
    cells = c.planned(system)
    if not cells:
        raise CampaignError(f"system {system!r} is not planned in {campaign_path}")
    deps = Deps(host=host, clock=clock)
    adapter = adapter_factory(system, None)
    host_facts = host()
    written = 0
    for planned_run, measure, item, track in cells:
        rid = (run_id + ("-trackb" if track == "B" else "")) if run_id else planned_run
        if is_done(root, system, measure, c.sf, item, rid):  # real data and manual failures stay
            continue
        write(_record(adapter, c, deps, host_facts, run_id=rid, measure=measure, item=item,
                      status="failed", track=track, reason=error, error=error,
                      extra={"repro": repro}), root)
        written += 1
    return written


# ---- the run ---------------------------------------------------------------------------

class _Run:
    def __init__(self, c: Campaign, root: Path, deps: Deps, data: DatasetPaths):
        self.c, self.root, self.deps, self.data = c, root, deps, data
        self.summary = Summary()
        self._reference: dict[int, dict] | None = None
        self._host: dict | None = None

    def say(self, msg: str) -> None:
        self.deps.out(msg)

    def host(self) -> dict:
        if self._host is None:
            self._host = self.deps.host()
        return self._host

    def write(self, rec: ResultRecord) -> None:
        write(rec, self.root)
        self.summary.written += 1

    def loaded(self, name: str, record_bulk: bool = False) -> SystemAdapter:
        """A started adapter with the schema created and the dataset bulk-loaded. With
        ``record_bulk`` the load is the M2 ``load_bulk`` measurement and its cell is written,
        also when the load fails; otherwise it is only setup for the other measures."""
        adapter = self.deps.adapter_factory(name, self.data.root)
        adapter.remove_data()
        try:
            adapter.start(self.c.limits)
            if record_bulk:
                rec = run_load_bulk(adapter, self.data, self.c.sf, self.c.run_id, self.root,
                                    clock=self.deps.clock, host=self.host)
                self.summary.written += 1
                if rec.status != "ok":
                    raise RuntimeError(f"bulk load failed: {rec.status} {rec.error} {rec.extra}")
                self.say(f"  loaded {name}: {rec.value:.1f}s")
            else:
                adapter.create_schema(self.c.sf, adapter.bench_schema_kind)
                load = adapter.load_bulk(self.data)
                if load.status != "ok":
                    raise RuntimeError(f"bulk load failed: {load.error} {load.extra}")
                self.say(f"  loaded {name}: {load.elapsed_s or 0:.1f}s")
        except BaseException:
            self._cleanup(adapter)
            raise
        return adapter

    def _cleanup(self, adapter: SystemAdapter) -> None:
        """Stop the system and delete its data. A failure is logged; when the server still
        answers afterwards the campaign is blocked rather than start the next system beside it."""
        try:
            adapter.remove_data()
        except Exception as exc:  # noqa: BLE001 - logged, then the probe decides
            self.say(f"  cleanup of {adapter.name} failed: {type(exc).__name__}: {exc}")
            try:
                running = adapter.alive()
            except Exception:  # noqa: BLE001 - an unreachable server is not running
                running = False
            if running:
                raise CampaignBlocked(
                    f"{adapter.name} is still running after a failed cleanup ({exc}); stop it "
                    f"(docker rm -f dbench-{adapter.name}) and run the campaign again to resume."
                ) from exc

    # reference
    def reference(self) -> dict[int, dict]:
        if self._reference is None:
            c = self.c
            self._reference = self._rows_of(c.reference_system, c.run_id)
            missing = [f"q{n:02d}" for n in QUERIES if n not in self._reference]
            if c.gated and missing:
                raise CampaignBlocked(
                    f"the reference {c.reference_system} has no result for {', '.join(missing)} "
                    "(error or timeout), so the baselines cannot be checked against it. Find "
                    "out why (run the query on plain DuckDB), then run the campaign again.")
            for other in c.cross_check_systems():
                self._cross_check(other)
        return self._reference

    def _rows_of(self, name: str, run_id: str) -> dict[int, dict]:
        cached = cached_reference(name, self.c.sf, run_id, self.root, QUERIES)
        if cached is not None:
            return cached
        self.say(f"reference rows from {name}")
        adapter = self.loaded(name)
        try:
            rows = build_reference(adapter, self.c.sf, run_id, self.root, QUERIES,
                                   self.c.timeout_s)
        finally:
            self._cleanup(adapter)
        missing = [n for n in QUERIES if n not in rows]
        if missing and not self.c.gated:
            self.say(f"  {name}: no result for queries {missing}; they are not compared")
        return rows

    def _cross_check(self, name: str) -> None:
        other = self._rows_of(name, f"{self.c.run_id}/_crosscheck")
        assert self._reference is not None
        bad = []
        for n, expected in self._reference.items():
            got = other.get(n)
            if got is None:
                continue
            numeric = set(expected["numeric_cols"]) | set(got["numeric_cols"])
            if not compare(expected["rows"], got["rows"], numeric).match:
                bad.append(n)
        for n in bad:
            self._write_disagreement(name, f"q{n:02d}", self.c.reference_system)
        if bad:
            items = ", ".join(f"q{n:02d}" for n in bad)
            raise CampaignBlocked(
                f"cross-check failed: {name} disagrees with {self.c.reference_system} on {items}. "
                "Resolve before continuing; the disagreement is recorded in "
                "extra.reference_disagreement.")

    def _write_disagreement(self, name: str, item: str, reference: str) -> None:
        adapter = self.deps.adapter_factory(name, None)
        rec = _record(
            adapter, self.c, self.deps, self.host(), run_id=self.c.run_id, measure="M1",
            item=item, status="wrong_result",
            extra={"reference_disagreement": {"system": name, "reference": reference,
                                              "query": item}})
        # kept beside the cross-check rows, never in the system's own Track A cells
        write(rec, self.root / self.c.run_id / "_crosscheck")
        self.summary.written += 1

    # systems
    def run_system(self, name: str) -> None:
        c = self.c
        adapter = self.deps.adapter_factory(name, self.data.root)
        todo: list[tuple[measures.Measure, list[str]]] = []
        for m in c.measures:
            measure = measures.get(m)
            items = measure.items(c.sf)
            pending = [i for i in items if not is_done(self.root, name, m, c.sf, i, c.run_id)]
            self.summary.skipped += len(items) - len(pending)
            if not pending:
                continue
            applies, reason = adapter.supports(m)
            if applies:
                todo.append((measure, pending))
                continue
            for item in pending:
                self.write(_record(adapter, c, self.deps, self.host(), run_id=c.run_id,
                                   measure=m, item=item, status="n/a", reason=reason))
        if todo:
            self._measure(adapter, todo)
        self._gate(name)

    def _failed(self, adapter: SystemAdapter, todo, reason: str) -> None:
        for measure, pending in todo:
            for item in pending:
                if is_done(self.root, adapter.name, measure.id, self.c.sf, item, self.c.run_id):
                    continue
                if (measure.id, item) == ("M2", measures.LOAD_BULK_ITEM) and cell_record(
                        self.root, adapter.name, "M2", self.c.sf, item, self.c.run_id):
                    continue  # the load's own (transient) result is more informative
                self.write(_record(adapter, self.c, self.deps, self.host(), run_id=self.c.run_id,
                                   measure=measure.id, item=item, status="failed",
                                   reason=reason, error=reason[:2000], extra={"auto": True}))

    def _context(self, measure: measures.Measure, pending: list[str],
                 reference: dict[int, dict] | None) -> measures.MeasureContext:
        c = self.c
        return measures.MeasureContext(
            sf=c.sf, run_id=c.run_id, results=self.root, data=self.data,
            reference=reference if measure.needs_reference else None,
            timeout_s=c.timeout_s, limits=c.limits, items=pending,
            drop_caches=self.deps.drop_caches, host=self.deps.host)

    def _measure(self, adapter: SystemAdapter, todo) -> None:
        """Bulk load (timed as M2 ``load_bulk`` when that cell is missing), M3 storage before
        any query, the query measures, then M2 ``load_convert`` on a fresh datadir."""
        reference = self.reference() if any(m.needs_reference for m, _ in todo) else None
        bulk_pending = any(m.id == "M2" and measures.LOAD_BULK_ITEM in p for m, p in todo)
        phases = {"after_load": [], "query": [], "last": []}
        for measure, pending in todo:
            items = [i for i in pending
                     if not (measure.id == "M2" and i == measures.LOAD_BULK_ITEM)]
            if items:
                phases[measure.phase].append((measure, items))
        started = adapter
        try:
            if bulk_pending or phases["after_load"] or phases["query"]:
                started = self.loaded(adapter.name, record_bulk=bulk_pending)
        except CampaignBlocked:
            raise
        except Exception as exc:  # noqa: BLE001 - a system that cannot start or load is a result
            self._failed(adapter, todo, f"{adapter.name} did not start or load: "
                                        f"{type(exc).__name__}: {exc}")
            return
        try:
            for phase in ("after_load", "query", "last"):
                for measure, pending in phases[phase]:
                    ctx = self._context(measure, pending, reference)
                    self.summary.written += len(measure.run(started, ctx))
        except HostSuspended as exc:  # cells already measured stay; the rest retry on resume
            self._failed(adapter, todo, f"{SUSPEND_REASON}: {exc}")
        except Exception as exc:  # noqa: BLE001 - record what did not finish, then go on
            self._failed(adapter, todo, f"{adapter.name} aborted {measure.id}: "
                                        f"{type(exc).__name__}: {exc}")
        finally:
            self._cleanup(started)

    def _gate(self, name: str) -> None:
        """At SF10 and below InnoDB and Postgres must agree with the reference."""
        if not self.c.gated or name not in GATE_SYSTEMS:
            return
        bad = [r for r in iter_records(self.root / self.c.run_id)
               if r.system == name and r.run_id == self.c.run_id and r.status == "wrong_result"]
        if not bad:
            return
        for r in bad:
            info = {"system": name, "reference": self.c.reference_system, "query": r.item}
            write(dataclasses.replace(r, extra={**r.extra, "reference_disagreement": info}),
                  self.root)
        items = ", ".join(r.item for r in bad)
        raise CampaignBlocked(
            f"{name} disagrees with {self.c.reference_system} on {items}. At SF{self.c.sf} "
            "InnoDB and Postgres must both match the reference: resolve this before "
            "continuing. The records carry extra.reference_disagreement; fix the cause, "
            "delete them and re-run to resume.")

    # track B
    def run_track_b(self) -> None:
        c = self.c
        items = trackb_items()
        pending = []
        for s in c.track_b_systems:
            done = sum(is_done(self.root, s, "M1", c.sf, i, c.trackb_run_id) for i in items)
            self.summary.skipped += done
            if done < len(items):
                pending.append(s)
        if not pending:
            return
        self.say(f"Track B ({c.trackb_run_id}): {', '.join(pending)}")
        records = self.deps.run_track_b(
            c.sf, c.trackb_run_id, self.root, pending, data=self.data, limits=c.limits,
            timeout_s=c.timeout_s)
        self.summary.written += len(records)


def _check_affinity(limits: Limits, deps: Deps) -> None:
    """The runner must itself be pinned inside runner_cpuset, or its work would steal cores
    from the database container."""
    allowed = dockerctl.parse_cpuset(limits.runner_cpuset)
    outside = sorted(set(deps.affinity()) - allowed)
    if outside:
        raise CampaignError(
            f"the runner may use CPUs {outside} outside runner_cpuset {limits.runner_cpuset!r}. "
            f"Start it pinned: DBENCH_RUNNER_CPUSET={limits.runner_cpuset} scripts/dev.sh "
            "dbench campaign <file>")


def run_campaign(path: Path, root: Path, deps: Deps | None = None) -> Summary:
    """Run the campaign file ``path``; results land under ``root/<run_id>/``."""
    deps = deps or Deps()
    c = load_campaign(path)
    cpus, mem_gb = deps.host_resources()
    dockerctl.validate_limits(c.limits, cpus, mem_gb)
    _check_affinity(c.limits, deps)
    deps.lock_check(sorted(c.lock_systems()))
    run = _Run(c, Path(root), deps, deps.dataset(c.sf))
    for name in c.ordered_systems():
        run.say(f"system {name}")
        run.run_system(name)
    if c.track_b_enabled:
        run.run_track_b()
    return run.summary
