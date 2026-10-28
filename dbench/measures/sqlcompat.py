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
    n/a            the row-store baseline that builds the reference, or a probe the host
                   server itself does not support (see the server control below)

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

Server control, the same rule for every system. The reference comes from one server per dialect
(MySQL 9.7 InnoDB, Postgres 18), but the integrations run on other servers (AliSQL, MariaDB,
Percona's MySQL 9.7, Postgres with an extension), so a probe can fail because the host server's
own dialect lacks the feature, before the DuckDB integration is involved. Whenever an engine
probe ends as ``error`` or ``wrong_result``, it is run once more on the same server and session
with every ``{{ engine_clause }}`` rendered as the ``rowstore_clause``: the same SQL on plain
row-store tables, without the integration. The control uses the probe's own tables and teardown
(the probe's tables are dropped before it starts and again after it), the same timeout and the
same crash/timeout recovery. If the control fails with an error too, or (for a wrong result)
returns the same rows as the integration and so also differs from the reference, the probe is
``n/a`` ("not supported by the <server> server itself ..."), with the integration's own outcome
kept in ``extra`` (``integration_class``, ``integration_error``, ``diffs``) and the control's in
``extra.server_control``. Otherwise the original class stands and ``extra.server_control`` is
``"ok"`` (or what the control saw, when it ran but differed from the reference, or crashed or
timed out, which never sets a probe aside). There is no control, and
``extra.server_control.skipped`` says why, when the engine and row-store clauses are the same,
when the probe reads TPC-H tables (engine tables everywhere), when the probe itself crashed or
timed out, when the server did not recover from it, or when its teardown failed (a leftover
table would fail the control for the wrong reason). The baselines get no control.
"""
from __future__ import annotations

import hashlib
import json
import re
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
from dbench.results import ResultRecord, Status, cell_record
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
CONTROLLED = ("error", "wrong_result")  # the outcomes that get a server control
CONTROL_REASON = "not supported by the {server} server itself ({how}): {detail}"
CONTROL_SAME_RESULT = "the row-store result differs from the reference in the same way"
# The TPC-H tables some read probes use; they are engine tables on every integration.
TPCH_TABLES = re.compile(r"\b(?:nation|region|supplier)\b", re.IGNORECASE)
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
    mixes: bool = False  # explicit mixed-engine marker (besides using both placeholders)

    def content_key(self, dialect: str) -> str:
        """Hash of everything that decides this dialect's reference result: the templates and
        their rendering for the dialect's row-store baseline, so a change to the probe, to the
        baseline's clauses or to ``render_sql`` never leaves a stale reference in use."""
        baseline = baseline_class(dialect)
        templates = [*self.statements("setup", dialect), *self.statements("teardown", dialect),
                     *self.main(dialect), *([self.check[dialect]] if self.check[dialect] else [])]
        doc = {"templates": templates, "rendered": [render_sql(t, baseline) for t in templates],
               "compare": self.compare, "expected": self.expected, "mixes": self.mixes}
        text = json.dumps(doc, sort_keys=True, default=str)
        return hashlib.sha256(text.encode()).hexdigest()[:16]

    def mixes_engines(self, dialect: str) -> bool:
        """The probe makes both an engine table and a row-store table."""
        text = " ".join(self.statements("setup", dialect) + self.main(dialect))
        return self.mixes or ("{{ engine_clause }}" in text and "{{ rowstore_clause }}" in text)

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
                 compare=entry.get("compare"), mixes=bool(entry.get("mixes")),
                 sql=sql, check=check, setup=entry.get("setup"),
                 teardown=entry.get("teardown"), expected=entry.get("expected"),
                 note=entry.get("note"))


def render_sql(sql: str, adapter: SystemAdapter | type[SystemAdapter]) -> str:
    """Fill ``{{ engine_clause }}`` and ``{{ rowstore_clause }}`` from the adapter."""
    return _render(sql, _clause(adapter, "engine_clause"), _clause(adapter, "rowstore_clause"))


def render_control_sql(sql: str, adapter: SystemAdapter | type[SystemAdapter]) -> str:
    """The server-control rendering: every table is a row-store table of the same server, so
    ``{{ engine_clause }}`` is filled with the adapter's ``rowstore_clause`` too."""
    rowstore = _clause(adapter, "rowstore_clause")
    return _render(sql, rowstore, rowstore)


def _clause(adapter: SystemAdapter | type[SystemAdapter], name: str) -> str:
    return getattr(adapter, name, "").strip()


def _render(sql: str, engine_clause: str, rowstore_clause: str) -> str:
    text = _ENV.from_string(sql).render(engine_clause=engine_clause,
                                        rowstore_clause=rowstore_clause)
    return text.rstrip()


def baseline_class(dialect: str) -> type[SystemAdapter]:
    """The adapter class of the row-store baseline that builds the reference for ``dialect``."""
    from dbench.systems.registry import _factories

    return _factories()[BASELINES[dialect]]


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

    def state(self, dialect: str, probe_id: str, key: str) -> str:
        """``ok`` (usable), ``stale`` (cached for another wording, key or scale factor) or
        ``absent`` (nothing cached)."""
        if self.load(dialect, probe_id, key) is not None:
            return "ok"
        return "stale" if self.load(dialect, probe_id) is not None else "absent"

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
    teardown_error: str | None = None  # the final teardown left a table behind


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


def _execute_probe(adapter: SystemAdapter, probe: Probe, timeout_s: int,
                   render: Callable[[str, SystemAdapter], str] = render_sql) -> _Observed:
    """Run one probe: leftover teardown, setup, the statement(s), the check, teardown. ``render``
    fills the clause placeholders (``render_control_sql`` for the server control)."""
    dialect = adapter.dialect
    obs = _Observed()
    teardown = [render(s, adapter) for s in probe.statements("teardown", dialect)]
    scratch = _Observed()
    try:
        # leftovers of an interrupted run
        _run_steps(adapter, teardown, timeout_s, scratch, stop_on_failure=False)
        failed = _run_steps(adapter, [render(s, adapter)
                                      for s in probe.statements("setup", dialect)],
                            timeout_s, obs)
        if failed:
            obs.failure, obs.stage = failed, "setup"
            return obs
        statements = [render(s, adapter) for s in probe.main(dialect)]
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
        res = adapter.execute(render(check, adapter), timeout_s)
        if res.status != "ok":
            obs.failure, obs.stage = res, "check"
            obs.restart_error = _recover(adapter, res) or obs.restart_error
            return obs
        obs.outcome = res
        return obs
    finally:
        left = _run_steps(adapter, teardown, timeout_s, scratch, stop_on_failure=False)
        obs.teardown_error = (left.error or left.status) if left else None
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
    extra: dict = {"class": "n/a", "outcome": "ok" if obs.outcome else obs.failure.status,
                   "reference_key": probe.content_key(dialect)}
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
    key = probe.content_key(dialect)
    state = store.state(dialect, probe.id, key)
    if probe.expected is None:
        extra["reference"] = state  # ok | stale | absent: why a result was or was not compared
        extra["reference_key"] = key if state == "ok" else None
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


def _reads_tpch(probe: Probe, dialect: str) -> bool:
    """The probe reads a TPC-H table (an engine table on every integration)."""
    check = [probe.check[dialect]] if probe.check[dialect] else []
    text = " ".join(probe.statements("setup", dialect) + probe.main(dialect) + check)
    return TPCH_TABLES.search(text) is not None


def _control_skip(adapter: SystemAdapter, probe: Probe, outcome: str | None,
                  restart_error: str | None, teardown_error: str | None = None) -> str | None:
    """Why a failed probe gets no server control (None when it gets one). ``outcome`` is the
    probe's ``extra.outcome``: ``error``/``timeout``/``crashed`` for an error, ``ok`` for a
    wrong result."""
    if _clause(adapter, "engine_clause") == _clause(adapter, "rowstore_clause"):
        return ("the engine and row-store clauses are the same: there is no row-store table "
                "to compare with")
    if _reads_tpch(probe, adapter.dialect):
        return ("the probe reads TPC-H tables, which are engine tables: a control would not "
                "run without the integration")
    if outcome in ("crashed", "timeout"):
        return f"the probe ended as {outcome}; a crash or a timeout is never set aside"
    if restart_error:
        return "the server did not recover after the probe"
    if teardown_error:
        return f"the probe's teardown failed, so its tables may still exist: {teardown_error}"
    return None


def _not_applicable(adapter: SystemAdapter, status: str, extra: dict, control: dict,
                    how: str, detail: str | None) -> tuple[str, dict, None, str]:
    """The record of a probe that the host server itself does not support: ``n/a``, with the
    integration's own outcome kept beside the control's."""
    reason = CONTROL_REASON.format(server=adapter.name, how=how, detail=detail or "")
    kept = {k: v for k, v in extra.items() if k != "error"}
    out = {**kept, "class": "n/a", "integration_class": status, "server_control": control}
    if status == "error":
        out["integration_error"] = extra.get("error")
    return "n/a", out, None, reason


def _server_control(adapter: SystemAdapter, probe: Probe, status: str, extra: dict,
                    error: str | None, obs: _Observed, store: ProbeReferenceStore,
                    timeout_s: int) -> tuple[str, dict, str | None, str | None]:
    """Re-run a failed probe on row-store tables of the same server, on the same session.

    When the server fails the same way without the integration (a clean error, or for a wrong
    result the same rows as the integration), the probe does not test the integration here and
    becomes ``n/a``. Otherwise the original class stands and ``extra.server_control`` says what
    the control saw. A control that crashes or times out proves nothing and changes nothing.
    Returns (status, extra, error, reason)."""
    skip = _control_skip(adapter, probe, extra.get("outcome"), obs.restart_error,
                         obs.teardown_error)
    if skip is not None:
        return status, {**extra, "server_control": {"skipped": skip}}, error, None
    ctl = _execute_probe(adapter, probe, timeout_s, render_control_sql)
    obs.restart_error = obs.restart_error or ctl.restart_error
    if ctl.outcome is None:
        fail = ctl.failure
        seen = {"outcome": fail.status, "stage": ctl.stage, "error": fail.error}
        if fail.status != "error":
            return status, {**extra, "server_control": {**seen, "conclusive": False}}, error, None
        how = ("same failure on a row-store table" if status == "error"
               else "fails on a row-store table")
        return _not_applicable(adapter, status, extra, seen, how, fail.error)
    res = ctl.outcome
    expected, numeric = _expected(probe, adapter.dialect, store, res.numeric_cols)
    if expected is None or compare(_prepared(probe, expected), _prepared(probe, res.rows or []),
                                   numeric).match:
        return status, {**extra, "server_control": "ok"}, error, None
    seen = {"outcome": "ok", "matches": False}
    if status == "wrong_result":
        mine = obs.outcome
        seen["same_as_integration"] = compare(
            _prepared(probe, res.rows or []), _prepared(probe, mine.rows or []),
            numeric | mine.numeric_cols).match
        if seen["same_as_integration"]:
            return _not_applicable(adapter, status, extra, seen,
                                   "same result on a row-store table", CONTROL_SAME_RESULT)
    return status, {**extra, "server_control": seen}, error, None


def _needs_control(adapter: SystemAdapter, probe: Probe, rec: ResultRecord) -> bool:
    """An engine record made before the server control existed, which would get one now."""
    return (rec.status in CONTROLLED and "server_control" not in rec.extra
            and _control_skip(adapter, probe, rec.extra.get("outcome"),
                              rec.extra.get("restart_error")) is None)


def _compared(rec: ResultRecord) -> bool:
    """The record's class depends on the reference it was compared with."""
    return rec.status in ("ok", "wrong_result") or (
        rec.status == "n/a" and rec.extra.get("integration_class") == "wrong_result")


def stale_items(adapter: SystemAdapter, root: Path, run_id: str, sf: int,
                probes: list[Probe] | None = None) -> set[str]:
    """Probe ids whose recorded M5 cell must run again although it exists, because the reference
    it was built from or compared with is not the current one: a baseline whose cache is missing
    or keyed to an older wording, or an engine whose result was compared with (or could not be
    compared for lack of) a reference that is now different, or an engine's failure recorded
    without the server control (``extra.server_control``) that it would get now."""
    dialect = adapter.dialect
    if dialect not in BASELINES:
        return set()
    store = ProbeReferenceStore(root, run_id, sf)
    baseline = adapter.name == BASELINES[dialect]
    stale: set[str] = set()
    for probe in load_probes() if probes is None else probes:
        rec = cell_record(root, adapter.name, "M5", sf, probe.id, run_id)
        if rec is None:
            continue
        key = probe.content_key(dialect)
        if baseline:
            built = rec.extra.get("reference_key") == key
            kept = rec.extra.get("baseline_error") is not None or (
                store.state(dialect, probe.id, key) == "ok")
            if not (built and kept):
                stale.add(probe.id)
        elif _needs_control(adapter, probe, rec) or (
                _compared(rec) and probe.expected is None
                and store.state(dialect, probe.id, key) == "ok"
                and rec.extra.get("reference_key") != key):
            stale.add(probe.id)
    return stale


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
            if status in CONTROLLED:
                status, extra, error, reason = _server_control(
                    adapter, probe, status, extra, error, obs, store, timeout_s)
        if probe.note:
            extra["note"] = probe.note
        if obs.restart_error:
            extra["restart_error"] = obs.restart_error
        records.append(make_record(
            adapter, measure="M5", item=probe.id, status=status, sf=sf, run_id=run_id,
            root=root, unit=UNIT, extra=extra, error=error, reason=reason,
            versions=versions, clock=clock, host=lambda: host_facts, started=started))
    return records
