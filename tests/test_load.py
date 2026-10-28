"""M2 (load paths) and M3 (storage after load and after checkpoint), with fakes."""
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from dbench.campaign import run_campaign
from dbench.config import Limits
from dbench.datagen import DatasetPaths
from dbench.measures import registry as measures
from dbench.measures.load import run_convert, run_load_bulk
from dbench.measures.storage import run_storage
from dbench.results import cell_record, is_done, iter_records
from dbench.systems.base import LoadOutcome
from tests.fakes import FakeAdapter
from tests.test_campaign import HOST, Env, Tracked, write_yaml

LIMITS = Limits("0-7", "8-11", 16)
BREAKDOWN = {"data_bytes": 70, "redo_wal_bytes": 20, "temp_bytes": 0, "other_bytes": 10}


class Clock:
    """Deterministic perf_counter: adapters advance it, the measure reads it."""

    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


class LoadFake(FakeAdapter):
    name = "loadfake"

    def __init__(self, clock: Clock | None = None, bulk: LoadOutcome | None = None,
                 convert: LoadOutcome | None = None, checkpoint_reason: str | None = None,
                 sizes: tuple[int, ...] = (500, 300)):
        super().__init__([])
        self.clock = clock or Clock()
        self.bulk = bulk or LoadOutcome("ok", 5.0, None, {"rows": {"t": 1}})
        self.convert = convert or LoadOutcome("ok", 7.0, None, {"rowstore_load_s": 40.0})
        self.checkpoint_reason = checkpoint_reason
        self.sizes = list(sizes)
        self.log: list[str] = []

    def remove_data(self) -> None: self.log.append("remove_data")
    def start(self, limits) -> None: self.log.append("start")

    def create_schema(self, sf, kind) -> None:
        self.log.append(f"create_schema:{kind}")
        self.clock.now += 3.0

    def load_bulk(self, data) -> LoadOutcome:
        self.log.append("load_bulk")
        self.clock.now += 5.0
        return self.bulk

    def load_convert(self, data) -> LoadOutcome:
        self.log.append("load_convert")
        return self.convert

    def checkpoint(self) -> str | None:
        self.log.append("checkpoint")
        return self.checkpoint_reason

    def storage_bytes(self) -> int:
        self.log.append("storage_bytes")
        return self.sizes.pop(0)

    def storage_breakdown(self) -> dict:
        return dict(BREAKDOWN)


def data_paths(tmp_path: Path) -> DatasetPaths:
    root = tmp_path / "data"
    return DatasetPaths(1, root, root / "csv", root / "parquet", root / "m.json")


COMMON = {"clock": lambda: datetime(2026, 1, 1, tzinfo=UTC), "host": lambda: HOST}


def bulk(adapter, tmp_path, **kw):
    return run_load_bulk(adapter, data_paths(tmp_path), 1, "run1", tmp_path / "res",
                         timer=getattr(adapter, "clock", Clock()), **COMMON, **kw)


# ---- M2 load_bulk ---------------------------------------------------------------------------

def test_bulk_time_covers_schema_and_load(tmp_path):
    a = LoadFake()
    rec = bulk(a, tmp_path)
    assert rec.measure == "M2" and rec.item == "load_bulk" and rec.status == "ok"
    assert rec.value == pytest.approx(8.0) and rec.unit == "s"  # 3 s schema + 5 s load
    assert rec.extra["rows_checked"] is True and rec.extra["rows"] == {"t": 1}
    assert a.log == ["create_schema:engine", "load_bulk"]
    assert cell_record(tmp_path / "res", "loadfake", "M2", 1, "load_bulk", "run1") == rec


def test_bulk_uses_the_systems_schema_kind(tmp_path):
    a = LoadFake()
    a.bench_schema_kind = "rowstore"
    bulk(a, tmp_path)
    assert a.log[0] == "create_schema:rowstore"


def test_row_count_mismatch_is_error(tmp_path):
    out = LoadOutcome("error", 4.0, "row count does not match the manifest",
                      {"kind": "row_count", "expected": {"t": 2}, "actual": {"t": 1}})
    rec = bulk(LoadFake(bulk=out), tmp_path)
    assert rec.status == "error" and rec.value is None
    assert rec.extra["kind"] == "row_count" and rec.extra["expected"] == {"t": 2}
    assert rec.error and "manifest" in rec.error


def test_bulk_crash_keeps_status_and_container_state(tmp_path):
    out = LoadOutcome("crashed", 9.0, "lost connection", {"container": {"status": "exited"}})
    rec = bulk(LoadFake(bulk=out), tmp_path)
    assert rec.status == "crashed" and rec.extra["container"] == {"status": "exited"}


# ---- M2 load_convert ------------------------------------------------------------------------

def convert(adapter, tmp_path):
    return run_convert(adapter, data_paths(tmp_path), 1, "run1", tmp_path / "res", LIMITS,
                       **COMMON)


def test_convert_excludes_rowstore_time(tmp_path):
    rec = convert(LoadFake(), tmp_path)
    assert rec.measure == "M2" and rec.item == "load_convert" and rec.status == "ok"
    assert rec.value == 7.0  # conversion only
    assert rec.extra["rowstore_load_s"] == 40.0


def test_convert_uses_fresh_datadir_and_removes_it(tmp_path):
    a = LoadFake()
    convert(a, tmp_path)
    assert a.log == ["remove_data", "start", "load_convert", "remove_data"]


def test_convert_cleans_up_when_it_raises(tmp_path):
    a = LoadFake()

    def boom(data):
        a.log.append("load_convert")
        raise RuntimeError("server went away")

    a.load_convert = boom
    with pytest.raises(RuntimeError):
        convert(a, tmp_path)
    assert a.log[-1] == "remove_data"


def test_convert_not_applicable_keeps_the_reason(tmp_path):
    a = LoadFake(convert=LoadOutcome("n/a", None, None, {"reason": "no row store to convert"}))
    rec = convert(a, tmp_path)
    assert rec.status == "n/a" and rec.reason == "no row store to convert"
    assert rec.value is None


def test_convert_row_count_mismatch_is_error(tmp_path):
    out = LoadOutcome("error", 1.0, "row count changed during conversion",
                      {"kind": "row_count", "rowstore_load_s": 3.0})
    rec = convert(LoadFake(convert=out), tmp_path)
    assert rec.status == "error" and rec.extra["kind"] == "row_count"
    assert rec.extra["rowstore_load_s"] == 3.0


# ---- M3 storage -----------------------------------------------------------------------------

def storage(adapter, tmp_path, items=("after_load", "after_checkpoint")):
    return run_storage(adapter, 1, "run1", tmp_path / "res", list(items), **COMMON)


def test_storage_measures_before_and_after_checkpoint(tmp_path):
    a = LoadFake(sizes=(500, 300))
    before, after = storage(a, tmp_path)
    assert a.log == ["storage_bytes", "checkpoint", "storage_bytes"]
    assert (before.item, before.value, before.unit) == ("after_load", 500, "bytes")
    assert (after.item, after.value) == ("after_checkpoint", 300)
    assert before.measure == after.measure == "M3" and {before.status, after.status} == {"ok"}
    assert before.extra["breakdown"] == BREAKDOWN
    assert after.extra["breakdown"] == BREAKDOWN


def test_checkpoint_na_reason(tmp_path):
    a = LoadFake(checkpoint_reason="InnoDB has no checkpoint statement")
    before, after = storage(a, tmp_path)
    assert before.status == "ok" and before.value == 500
    assert after.status == "n/a" and after.value is None
    assert after.reason == "InnoDB has no checkpoint statement"
    assert a.log == ["storage_bytes", "checkpoint"]  # no second measurement


def test_storage_failure_is_a_recorded_error(tmp_path):
    a = LoadFake()

    def broken() -> int:
        raise RuntimeError("du failed (1): no such container")

    a.storage_bytes = broken
    before, after = storage(a, tmp_path)
    assert before.status == "error" and "du failed" in before.error
    assert after.status == "error"


def test_storage_only_runs_pending_items(tmp_path):
    a = LoadFake(sizes=(300,))
    (rec,) = storage(a, tmp_path, items=("after_checkpoint",))
    assert rec.item == "after_checkpoint" and rec.value == 300
    assert a.log == ["checkpoint", "storage_bytes"]


def test_m2_and_m3_are_registered():
    assert {"M2", "M3"} <= set(measures.registered())
    assert measures.get("M2").unit == "s" and measures.get("M3").unit == "bytes"
    assert measures.get("M2").items(1) == ["load_bulk", "load_convert"]
    assert measures.get("M3").items(1) == ["after_load", "after_checkpoint"]


# ---- campaign integration -------------------------------------------------------------------

class LoadTracked(Tracked):
    """Tracked adapter that logs the load and storage steps into the shared event list."""

    CONVERTING = {"alisql"}

    def __init__(self, name, events, **kw):
        super().__init__(name, events, **kw)
        self.bulk_ok = True

    def create_schema(self, sf, kind): self._events.append(("schema", self.name))

    def load_bulk(self, data):
        self._events.append(("load_bulk", self.name))
        if not self.bulk_ok:
            return LoadOutcome("error", 1.0, "row count does not match the manifest",
                               {"kind": "row_count"})
        return LoadOutcome("ok", 2.0, None, {"rows": {"t": 1}})

    def load_convert(self, data):
        self._events.append(("load_convert", self.name))
        if self.name in self.CONVERTING:
            return LoadOutcome("ok", 4.0, None, {"rowstore_load_s": 9.0})
        return LoadOutcome("n/a", None, None, {"reason": "baseline: nothing to convert"})

    def checkpoint(self):
        self._events.append(("checkpoint", self.name))
        return None

    def storage_bytes(self):
        self._events.append(("storage", self.name))
        return 1000

    def storage_breakdown(self):
        return dict(BREAKDOWN)

    def start(self, limits):
        super().start(limits)
        self._events.append(("started", self.name))


class LoadEnv(Env):
    def __init__(self, tmp_path, failing_bulk=(), **kw):
        super().__init__(tmp_path, **kw)
        self.failing_bulk = set(failing_bulk)

    def factory(self, name, data_dir):
        a = LoadTracked(name, self.events, rows=self.rows.get(name), supported=True)
        a.bulk_ok = name not in self.failing_bulk
        return a


def campaign(tmp_path, env, **over):
    over.setdefault("measures", ["M1", "M2", "M3"])
    over.setdefault("systems", ["innodb", "postgres", "alisql"])
    res = tmp_path / "res"
    run_campaign(write_yaml(tmp_path, **over), res, env.deps())
    return res


def of(res, system, measure=None):
    return {r.item: r for r in iter_records(res) if r.system == system
            and (measure is None or r.measure == measure)}


def test_campaign_records_m2_and_m3_for_every_system(tmp_path):
    env = LoadEnv(tmp_path)
    res = campaign(tmp_path, env)
    for system in ("innodb", "postgres", "alisql"):
        m2 = of(res, system, "M2")
        assert m2["load_bulk"].status == "ok" and m2["load_bulk"].value > 0
        assert m2["load_bulk"].extra["rows_checked"] is True
        m3 = of(res, system, "M3")
        assert (m3["after_load"].value, m3["after_checkpoint"].value) == (1000, 1000)
        assert m3["after_load"].extra["breakdown"] == BREAKDOWN
    assert of(res, "alisql", "M2")["load_convert"].value == 4.0
    assert of(res, "alisql", "M2")["load_convert"].extra["rowstore_load_s"] == 9.0
    na = of(res, "innodb", "M2")["load_convert"]
    assert na.status == "n/a" and na.reason == "baseline: nothing to convert"


def test_m3_is_measured_before_the_first_query(tmp_path):
    env = LoadEnv(tmp_path)
    campaign(tmp_path, env, systems=["innodb", "postgres", "alisql"])
    mine = [k for k, n in env.events if n == "alisql"]
    assert mine.index("storage") < mine.index("query")
    assert mine.index("load_bulk") < mine.index("storage") < mine.index("checkpoint")
    assert mine.count("load_bulk") == 1


def test_convert_runs_last_on_a_fresh_datadir(tmp_path):
    env = LoadEnv(tmp_path)
    campaign(tmp_path, env)
    mine = [k for k, n in env.events if n == "alisql"]
    convert_at = mine.index("load_convert")
    assert mine.index("query") < convert_at
    # a second start, after the bulk-load datadir was removed
    assert mine[:convert_at].count("started") == 2
    assert mine[-1] == "stop"
    assert mine.count("stop") == 2


def test_resume_does_not_rerecord_load_bulk(tmp_path):
    env = LoadEnv(tmp_path)
    res = campaign(tmp_path, env)
    bulk_path = res / "r1" / "alisql" / "M2-sf1-load_bulk.json"
    before = bulk_path.read_text()
    (res / "r1" / "alisql" / "M1-sf1-q05.json").unlink()

    later = datetime(2027, 1, 1, tzinfo=UTC)
    env2 = LoadEnv(tmp_path)
    deps = env2.deps()
    deps.clock = lambda: later
    run_campaign(write_yaml(tmp_path, measures=["M1", "M2", "M3"],
                            systems=["innodb", "postgres", "alisql"]), res, deps)
    mine = [k for k, n in env2.events if n == "alisql"]
    assert mine.count("load_bulk") == 1  # reloaded to run q05 ...
    assert bulk_path.read_text() == before  # ... but not re-recorded
    assert "load_convert" not in mine and "storage" not in mine
    assert mine.count("started") == 1


def test_missing_m2_cell_is_remeasured_on_resume(tmp_path):
    env = LoadEnv(tmp_path)
    res = campaign(tmp_path, env)
    (res / "r1" / "alisql" / "M2-sf1-load_bulk.json").unlink()
    (res / "r1" / "alisql" / "M3-sf1-after_load.json").unlink()
    env2 = LoadEnv(tmp_path)
    campaign(tmp_path, env2)
    assert of(res, "alisql", "M2")["load_bulk"].status == "ok"
    assert "after_load" in of(res, "alisql", "M3")
    assert "query" not in [k for k, n in env2.events if n == "alisql"]


def test_only_convert_missing_skips_the_bulk_load(tmp_path):
    env = LoadEnv(tmp_path)
    res = campaign(tmp_path, env)
    (res / "r1" / "alisql" / "M2-sf1-load_convert.json").unlink()
    env2 = LoadEnv(tmp_path)
    campaign(tmp_path, env2)
    mine = [k for k, n in env2.events if n == "alisql"]
    assert "load_bulk" not in mine and mine.count("load_convert") == 1
    assert is_done(res, "alisql", "M2", 1, "load_convert", "r1")


def test_bulk_load_failure_records_error_and_fails_the_rest(tmp_path):
    env = LoadEnv(tmp_path, failing_bulk={"alisql"})
    res = campaign(tmp_path, env)
    recs = of(res, "alisql")
    bulk_rec = recs["load_bulk"]
    assert bulk_rec.status == "error" and bulk_rec.extra["kind"] == "row_count"
    assert not bulk_rec.extra.get("auto")
    others = [r for i, r in recs.items() if i != "load_bulk"]
    assert others and {r.status for r in others} == {"failed"}
    assert all(r.extra.get("auto") for r in others)
    assert "query" not in [k for k, n in env.events if n == "alisql"]
