"""M5: SQL compatibility probes.

``probes/probes.yaml`` holds about 100 statements that a user of an OLTP database would naturally
try (types, functions, joins, subqueries, CTEs, window functions, DML, DDL, session variables).
Each probe has a MySQL and a PostgreSQL spelling of the same intent. A probe runs on one system
and gets one class (see ``docs/methodology/sql-compat.md``):

    duckdb         ran, and the observed statement was pushed to DuckDB (read probes only)
    fallback       ran on the row path
    error          failed (any stage: setup, statement, follow-up check)
    wrong_result   ran, and the result differs from the expected one
    unknown        ran, but pushdown cannot be told (no counter, or the counter was unreadable)
    ok_unobserved  a DML/DDL probe that succeeded and whose follow-up check matched
    n/a            the row-store baseline that builds the reference

The expected result of a probe comes from the same-dialect row-store baseline (InnoDB for the
MySQL dialect, Postgres for the PostgreSQL one), cached under
``<results>/<run_id>/_probe_reference/``, unless the probe carries a literal ``expected``.

Probes work on their own scratch tables, created in ``setup`` with ``{{ engine_clause }}`` (the
system's engine: ``ENGINE=DuckDB``, ``USING ducklake``, or nothing) or ``{{ rowstore_clause }}``
(a plain row-store table, for mixed-engine joins) and dropped in ``teardown``. DML and DDL probes
carry a follow-up SELECT (``check_*``) that supplies the result that is compared; their class is
``ok_unobserved`` when it matches (the status counters only watch SELECTs) and the check's
pushdown is kept as ``extra.check_pushed``. A probe that uses both placeholders mixes an engine
table with a row-store table; where the two clauses are the same (pg_duckdb) there is nothing to
mix and it is ``n/a``.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from jinja2 import Environment, StrictUndefined

from dbench import hostinfo
from dbench.correctness import compare
from dbench.measures.records import make_record
from dbench.reference import encode
from dbench.results import ResultRecord, Status
from dbench.systems.base import QueryOutcome, SystemAdapter
from dbench.systems.duckdb_native import decode

CATEGORIES = ("types", "functions", "joins", "subqueries", "cte", "window", "dml", "ddl",
              "variables")
CLASSES = ("duckdb", "fallback", "error", "wrong_result", "unknown", "ok_unobserved", "n/a")
DIALECTS = ("mysql", "postgres")
BASELINES = {"mysql": "innodb", "postgres": "postgres"}  # the reference system per dialect
WRITE_CATEGORIES = ("dml", "ddl")
PROBE_TIMEOUT_S = 60
PROBES_FILE = Path(__file__).resolve().parents[2] / "probes" / "probes.yaml"
BASELINE_REASON = "row-store baseline; reference for the probe suite"
NO_MIX_REASON = ("{system} queries heap tables; there is no separate engine table to mix with")
COMPARE_MODES = ("json",)
UNIT = "class"

_ENV = Environment(undefined=StrictUndefined)


class ProbeFileError(ValueError):
    """The probe file is malformed."""


@dataclass(frozen=True)
class Probe:
    id: str
    category: str
    title: str
    sql: dict[str, Any]  # dialect -> statement, or a list run on one session (last = probe)
    check: dict[str, str | None]  # dialect -> follow-up SELECT (required for dml/ddl)
    setup: Any = None  # list (both dialects) or {dialect: list}
    teardown: Any = None
    expected: list | None = None
    note: str | None = None
    compare: str | None = None  # "json": cells holding JSON text are compared canonically

    def content_key(self, dialect: str) -> str:
        """Hash of everything that decides this dialect's result (the unrendered templates), so
        a cached reference built from an older wording of the probe is never reused."""
        doc = {"setup": self.statements("setup", dialect), "sql": self.main(dialect),
               "check": self.check[dialect], "compare": self.compare,
               "expected": self.expected}
        return hashlib.sha256(json.dumps(doc, sort_keys=True).encode()).hexdigest()[:16]

    def mixes_engines(self, dialect: str) -> bool:
        """The probe makes both an engine table and a row-store table."""
        text = " ".join(self.statements("setup", dialect) + self.main(dialect))
        return "{{ engine_clause }}" in text and "{{ rowstore_clause }}" in text

    def statements(self, kind: str, dialect: str) -> list[str]:
        """``setup`` or ``teardown`` of this dialect, as a flat list of templates."""
        return _flat(getattr(self, kind), dialect)

    def main(self, dialect: str) -> list[str]:
        return _flat(self.sql[dialect], dialect)

    @property
    def writes(self) -> bool:
        return self.category in WRITE_CATEGORIES


def _flat(value: Any, dialect: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, dict):
        return _flat(value.get(dialect), dialect)
    if isinstance(value, str):
        return [value]
    return [s for item in value for s in _flat(item, dialect)]


def load_probes(path: Path | None = None) -> list[Probe]:
    """Parse and validate the probe file."""
    path = Path(path) if path else PROBES_FILE
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ProbeFileError(f"cannot read {path}: {exc}") from exc
    entries = doc.get("probes") if isinstance(doc, dict) else doc
    if not isinstance(entries, list) or not entries:
        raise ProbeFileError(f"{path} must hold a list of probes")
    probes, seen = [], set()
    for entry in entries:
        probe = _parse(entry)
        if probe.id in seen:
            raise ProbeFileError(f"duplicate probe id {probe.id!r}")
        seen.add(probe.id)
        probes.append(probe)
    return probes


def _parse(entry: Any) -> Probe:
    if not isinstance(entry, dict) or not isinstance(entry.get("id"), str):
        raise ProbeFileError(f"probe without an id: {entry!r}")
    pid = entry["id"]
    if entry.get("category") not in CATEGORIES:
        raise ProbeFileError(f"{pid}: category must be one of {list(CATEGORIES)}")
    sql = {d: entry.get(f"sql_{d}") for d in DIALECTS}
    check = {d: entry.get(f"check_{d}") for d in DIALECTS}
    for d in DIALECTS:
        if not _flat(sql[d], d):
            raise ProbeFileError(f"{pid}: sql_{d} is missing")
        if entry["category"] in WRITE_CATEGORIES and not check[d]:
            raise ProbeFileError(f"{pid}: a {entry['category']} probe needs check_{d}")
    if entry.get("compare") not in (None, *COMPARE_MODES):
        raise ProbeFileError(f"{pid}: compare must be one of {list(COMPARE_MODES)}")
    return Probe(id=pid, category=entry["category"], title=str(entry.get("title") or ""),
                 compare=entry.get("compare"),
                 sql=sql, check=check, setup=entry.get("setup"),
                 teardown=entry.get("teardown"), expected=entry.get("expected"),
                 note=entry.get("note"))


def render_sql(sql: str, adapter: SystemAdapter | type[SystemAdapter]) -> str:
    """Fill ``{{ engine_clause }}`` and ``{{ rowstore_clause }}`` from the adapter."""
    text = _ENV.from_string(sql).render(
        engine_clause=getattr(adapter, "engine_clause", "").strip(),
        rowstore_clause=getattr(adapter, "rowstore_clause", "").strip())
    return text.rstrip()


def classify_probe(*, status: str, pushed: str, scope: str, matches: bool | None,
                   write: bool) -> str:
    """Class of a probe from the observed statement's outcome. ``matches`` is None when no
    expected result exists. A failure is an error whatever else is known; a wrong result is
    reported even when the statement was pushed. A write probe that is correct is
    ``ok_unobserved``: its pushdown is not what the follow-up SELECT's counter measures."""
    if status != "ok":
        return "error"
    if matches is False:
        return "wrong_result"
    if write:
        return "ok_unobserved"
    if scope != "none" and pushed in ("duckdb", "fallback"):
        return pushed
    return "unknown"


class ProbeReferenceStore:
    """Baseline results cached as ``<root>/<run_id>/_probe_reference/<probe>.<dialect>.json``,
    in the typed encoding of ``dbench.reference`` so Decimal and date values survive."""

    def __init__(self, root: Path, run_id: str, sf: int):
        self.dir = Path(root) / run_id / "_probe_reference"
        self.sf = sf

    def _file(self, dialect: str, probe_id: str) -> Path:
        return self.dir / f"{probe_id}.{dialect}.json"

    def save(self, dialect: str, probe_id: str, system: str, rows: list[tuple],
             numeric_cols: Iterable[int], columns: list[str], key: str | None = None) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        doc = {"system": system, "sf": self.sf, "dialect": dialect,
               "rows": [[encode(v) for v in row] for row in rows],
               "numeric_cols": sorted(numeric_cols), "columns": list(columns), "key": key}
        path = self._file(dialect, probe_id)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(doc), encoding="utf-8")
        tmp.replace(path)

    def load(self, dialect: str, probe_id: str, key: str | None = None) -> dict | None:
        """The cached result, or None when missing, for another scale factor, or (when ``key`` is
        given) built from a different wording of the probe."""
        try:
            doc = json.loads(self._file(dialect, probe_id).read_text(encoding="utf-8"))
            if doc["sf"] != self.sf or (key is not None and doc.get("key") != key):
                return None
            return {"rows": [tuple(decode(v) for v in row) for row in doc["rows"]],
                    "numeric_cols": set(doc["numeric_cols"]), "columns": list(doc["columns"]),
                    "system": doc["system"]}
        except (OSError, ValueError, KeyError, TypeError):
            return None


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass
class _Observed:
    """What running one probe produced: the observed statement's outcome, or the failure."""

    outcome: QueryOutcome | None = None
    failure: QueryOutcome | None = None
    stage: str | None = None
    restart_error: str | None = None


def _recover(adapter: SystemAdapter, outcome: QueryOutcome) -> str | None:
    """Make the server usable after a crash or a timeout; the restart's error text if it fails."""
    try:
        if outcome.status == "crashed":
            adapter.restart()
        elif outcome.status == "timeout":
            adapter.cancel()
    except Exception as exc:  # noqa: BLE001 - recorded, never raised
        return f"{type(exc).__name__}: {exc}"
    return None


def _run_steps(adapter: SystemAdapter, steps: list[str], timeout_s: int, obs: _Observed,
               *, stop_on_failure: bool = True) -> QueryOutcome | None:
    """Run setup/teardown statements; the first failure is kept in ``obs`` and returned. Teardown
    passes ``stop_on_failure=False`` so one failing DROP does not leave the other tables behind."""
    first: QueryOutcome | None = None
    for stmt in steps:
        res = adapter.execute(stmt, timeout_s)
        if res.status != "ok":
            obs.restart_error = _recover(adapter, res) or obs.restart_error
            first = first or res
            if stop_on_failure:
                break
    return first


def _canonical_json(value):
    """JSON text (or an already parsed document) as sorted, whitespace-free JSON; anything that
    is not a JSON object or array is returned unchanged."""
    if isinstance(value, (dict, list)):
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    if isinstance(value, str) and value.lstrip()[:1] in ("{", "["):
        try:
            return json.dumps(json.loads(value), sort_keys=True, separators=(",", ":"),
                              ensure_ascii=False)
        except ValueError:
            return value
    return value


def _prepared(probe: Probe, rows: list[tuple]) -> list[tuple]:
    """Rows as compared: with ``compare: json``, JSON cells are canonicalised first."""
    if probe.compare != "json":
        return list(rows)
    return [tuple(_canonical_json(v) for v in row) for row in rows]


def _execute_probe(adapter: SystemAdapter, probe: Probe, timeout_s: int) -> _Observed:
    dialect = adapter.dialect
    obs = _Observed()
    teardown = [render_sql(s, adapter) for s in probe.statements("teardown", dialect)]
    scratch = _Observed()
    try:
        # leftovers of an interrupted run
        _run_steps(adapter, teardown, timeout_s, scratch, stop_on_failure=False)
        failed = _run_steps(adapter, [render_sql(s, adapter)
                                      for s in probe.statements("setup", dialect)],
                            timeout_s, obs)
        if failed:
            obs.failure, obs.stage = failed, "setup"
            return obs
        statements = [render_sql(s, adapter) for s in probe.main(dialect)]
        main = (adapter.execute(statements[0], timeout_s) if len(statements) == 1
                else adapter.execute_session(statements, timeout_s))
        if main.status != "ok":
            obs.failure, obs.stage = main, "statement"
            obs.restart_error = _recover(adapter, main) or obs.restart_error
            return obs
        check = probe.check[dialect]
        if not check:
            obs.outcome = main
            return obs
        res = adapter.execute(render_sql(check, adapter), timeout_s)
        if res.status != "ok":
            obs.failure, obs.stage = res, "check"
            obs.restart_error = _recover(adapter, res) or obs.restart_error
            return obs
        obs.outcome = res
        return obs
    finally:
        _run_steps(adapter, teardown, timeout_s, scratch, stop_on_failure=False)
        obs.restart_error = obs.restart_error or scratch.restart_error


def _diff_rows(diffs: list[tuple]) -> list[list]:
    return [[pos, repr(a), repr(b)] for pos, a, b in diffs]


def _expected(probe: Probe, dialect: str, store: ProbeReferenceStore,
              numeric: set[int]) -> tuple[list | None, set[int]]:
    if probe.expected is not None:
        return [tuple(r) for r in probe.expected], numeric
    cached = store.load(dialect, probe.id, probe.content_key(dialect))
    if cached is None:
        return None, numeric
    return cached["rows"], numeric | cached["numeric_cols"]


def _baseline_extra(probe: Probe, dialect: str, system: str, obs: _Observed,
                    store: ProbeReferenceStore) -> dict:
    """The record of a baseline run: it builds the reference and checks it against the other
    baseline's result for the same probe (a disagreement means the probe is not equivalent)."""
    extra: dict = {"class": "n/a", "outcome": "ok" if obs.outcome else obs.failure.status}
    if obs.outcome is None:
        extra["baseline_error"] = obs.failure.error
        extra["stage"] = obs.stage
        return extra
    rows = obs.outcome.rows or []
    store.save(dialect, probe.id, system, rows, obs.outcome.numeric_cols, obs.outcome.columns,
               probe.content_key(dialect))
    other_dialect = "postgres" if dialect == "mysql" else "mysql"
    other = store.load(other_dialect, probe.id, probe.content_key(other_dialect))
    if other is not None:
        numeric = obs.outcome.numeric_cols | other["numeric_cols"]
        cmp = compare(_prepared(probe, other["rows"]), _prepared(probe, rows), numeric)
        extra["baseline_disagreement"] = not cmp.match
        if not cmp.match:
            extra["diffs"] = _diff_rows(cmp.diffs)
            extra["other_baseline"] = other["system"]
    return extra


def _probe_extra(probe: Probe, adapter: SystemAdapter, obs: _Observed,
                 store: ProbeReferenceStore) -> tuple[str, str, dict, str | None]:
    """(status, class, extra, error) of an engine probe."""
    dialect = adapter.dialect
    extra: dict = {"category": probe.category, "title": probe.title}
    if obs.outcome is None:
        fail = obs.failure
        extra.update({"class": "error", "stage": obs.stage, "outcome": fail.status,
                      "error": fail.error})
        return "error", "error", extra, fail.error
    res = obs.outcome
    expected, numeric = _expected(probe, dialect, store, res.numeric_cols)
    matches = None
    if expected is not None:
        cmp = compare(_prepared(probe, expected), _prepared(probe, res.rows or []), numeric)
        matches = cmp.match
        if not cmp.match:
            extra["row_count"] = [cmp.row_count_a, cmp.row_count_b]
            extra["diffs"] = _diff_rows(cmp.diffs)
    cls = classify_probe(status="ok", pushed=res.pushed, scope=adapter.pushdown_counter_scope,
                         matches=matches, write=probe.writes)
    extra.update({"class": cls, "outcome": "ok", "compared": expected is not None, "error": None})
    # A write probe's follow-up SELECT is not the statement under test: its pushdown is recorded
    # as a fact about the check and kept out of the pushdown shares (M8).
    extra["check_pushed" if probe.writes else "pushed"] = res.pushed
    return ("wrong_result" if cls == "wrong_result" else "ok"), cls, extra, None


def _nothing_to_mix(adapter: SystemAdapter, probe: Probe) -> bool:
    """A mixed-engine probe on a system whose engine and row-store clauses are the same: both
    tables would be the same kind, so the probe would not test mixing."""
    same = (getattr(adapter, "engine_clause", "").strip()
            == getattr(adapter, "rowstore_clause", "").strip())
    return same and probe.mixes_engines(adapter.dialect)


def run_sqlcompat(
    adapter: SystemAdapter,
    run_id: str,
    root: Path,
    *,
    sf: int = 1,
    probes: list[Probe] | None = None,
    only: Iterable[str] | None = None,
    timeout_s: int = PROBE_TIMEOUT_S,
    clock: Callable[[], datetime] = _utcnow,
    host: Callable[[], dict] = hostinfo.host_info,
) -> list[ResultRecord]:
    """Run the probes on ``adapter``, write one M5 record per probe and return them. The
    row-store baseline of the adapter's dialect is recorded as ``n/a`` and caches its results as
    the reference the engines are compared with."""
    probes = load_probes() if probes is None else probes
    wanted = None if only is None else set(only)
    dialect = adapter.dialect
    store = ProbeReferenceStore(root, run_id, sf)
    baseline = adapter.name == BASELINES.get(dialect)
    host_facts = host()
    versions = adapter.versions()
    records: list[ResultRecord] = []
    for probe in probes:
        if wanted is not None and probe.id not in wanted:
            continue
        started = clock()
        status: Status
        error = reason = None
        if not baseline and _nothing_to_mix(adapter, probe):
            reason = NO_MIX_REASON.format(system=adapter.name)
            records.append(make_record(
                adapter, measure="M5", item=probe.id, status="n/a", sf=sf, run_id=run_id,
                root=root, unit=UNIT, reason=reason, extra={
                    "class": "n/a", "category": probe.category, "title": probe.title},
                versions=versions, clock=clock, host=lambda: host_facts, started=started))
            continue
        obs = _execute_probe(adapter, probe, timeout_s)
        if baseline:
            status, reason = "n/a", BASELINE_REASON
            extra = {"category": probe.category, "title": probe.title,
                     **_baseline_extra(probe, dialect, adapter.name, obs, store)}
        else:
            status, _, extra, error = _probe_extra(probe, adapter, obs, store)
        if probe.note:
            extra["note"] = probe.note
        if obs.restart_error:
            extra["restart_error"] = obs.restart_error
        records.append(make_record(
            adapter, measure="M5", item=probe.id, status=status, sf=sf, run_id=run_id,
            root=root, unit=UNIT, extra=extra, error=error, reason=reason,
            versions=versions, clock=clock, host=lambda: host_facts, started=started))
    return records
