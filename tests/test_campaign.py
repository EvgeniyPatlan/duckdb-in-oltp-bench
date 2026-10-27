"""Campaign runner: resume, n/a, failure marking, pre-start checks, reference gate (fakes)."""
from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from dbench import lock
from dbench.campaign import (
    Campaign,
    CampaignBlocked,
    CampaignError,
    Deps,
    load_campaign,
    mark_failed,
    run_campaign,
)
from dbench.config import Limits
from dbench.datagen import DatasetPaths
from dbench.measures import registry as measures
from dbench.results import exists, iter_records
from tests.fakes import FakeAdapter, ok

HOST = {"cpus": 20, "mem_gb": 62, "kernel": "k", "docker_version": "d"}
NOW = datetime(2026, 1, 1, tzinfo=UTC)


def write_yaml(tmp_path: Path, **over) -> Path:
    doc = {
        "run_id": "r1", "sf": 1,
        "systems": ["duckdb-1.5.5", "innodb", "postgres", "alisql"],
        "measures": ["M1"],
        "limits": {"db_cpuset": "0-7", "runner_cpuset": "8-11", "mem_gb": 16},
        "reference": {"system": "duckdb-1.5.5", "cross_check": ["duckdb-1.4.3"]},
        "track_b": {"enabled": False, "systems": ["duckdb-1.5.5", "villagesql"]},
    }
    doc.update(over)
    path = tmp_path / "c.yaml"
    path.write_text(json.dumps(doc))  # JSON is YAML
    return path


class Tracked(FakeAdapter):
    """FakeAdapter that logs lifecycle events into a shared list."""

    def __init__(self, name: str, events: list, rows=None, supported: bool = True):
        super().__init__([])
        self.name = name
        baseline = name in ("innodb", "postgres") or name.startswith("duckdb")
        self.track = "baseline" if baseline else "A"
        self._events = events
        self._rows = rows
        self._supported = supported
        self.running = False

    def start(self, limits):
        open_now = sum(1 for e in self._events if e[0] == "start") - sum(
            1 for e in self._events if e[0] == "stop")
        assert open_now == 0, "two systems at once"
        self.running = True
        self._events.append(("start", self.name))

    def stop(self):
        self.running = False

    def remove_data(self):
        if self.running:
            self._events.append(("stop", self.name))
        self.running = False

    def execute(self, sql, timeout_s):
        self._events.append(("query", self.name))
        return ok(1.0, self._rows)

    def supports(self, measure):
        return (True, None) if self._supported else (False, "not supported here")


class Env:
    """Fake dependencies recording what ran."""

    def __init__(self, tmp_path: Path, rows: dict | None = None, unsupported=(), host=(20, 62)):
        self.events: list = []
        self.rows = rows or {}
        self.unsupported = set(unsupported)
        self.host = host
        self.trackb_calls: list = []
        self.lock_calls: list = []
        self.tmp = tmp_path

    def factory(self, name, data_dir):
        return Tracked(name, self.events, self.rows.get(name), name not in self.unsupported)

    def dataset(self, sf):
        root = self.tmp / "data"
        return DatasetPaths(sf, root, root / "csv", root / "parquet", root / "m.json")

    def trackb(self, sf, run_id, root, systems, **kw):
        self.trackb_calls.append((sf, run_id, list(systems), kw))
        return []

    def deps(self) -> Deps:
        return Deps(
            adapter_factory=self.factory,
            host_resources=lambda: self.host,
            lock_check=lambda systems: self.lock_calls.append(sorted(systems)),
            dataset=self.dataset,
            run_track_b=self.trackb,
            drop_caches=lambda: None,
            host=lambda: HOST,
            clock=lambda: NOW,
            out=lambda msg: None,
        )

    def started(self) -> list[str]:
        return [n for kind, n in self.events if kind == "start"]


def records(root: Path, system: str | None = None):
    return [r for r in iter_records(root) if system is None or r.system == system]


def test_campaign_runs_all_cells(tmp_path):
    env = Env(tmp_path)
    run_campaign(write_yaml(tmp_path), tmp_path / "res", env.deps())
    assert len(records(tmp_path / "res", "alisql")) == 22
    assert {r.status for r in iter_records(tmp_path / "res")} == {"ok"}
    assert len(records(tmp_path / "res")) == 4 * 22
    assert (tmp_path / "res" / "r1" / "_reference" / "q01.json").is_file()


def test_resume_skips_existing(tmp_path):
    res = tmp_path / "res"
    env = Env(tmp_path)
    path = write_yaml(tmp_path)
    run_campaign(path, res, env.deps())
    before = {p: p.read_text() for p in res.rglob("M1-*.json")}
    assert len(before) == 88

    # one record lost: only that cell reruns, the rest is untouched and nothing is duplicated
    victim = res / "r1" / "alisql" / "M1-sf1-q05.json"
    victim.unlink()
    env2 = Env(tmp_path)
    summary = run_campaign(path, res, env2.deps())
    after = {p: p.read_text() for p in res.rglob("M1-*.json")}
    assert len(after) == 88 and victim.is_file()
    assert {p: t for p, t in after.items() if p != victim} == {
        p: t for p, t in before.items() if p != victim}
    assert env2.started() == ["alisql"]  # complete systems are not even started
    assert sum(1 for kind, n in env2.events if kind == "query" and n == "alisql") == 4
    assert summary.skipped == 87 and summary.written == 1

    # fully complete: nothing starts
    env3 = Env(tmp_path)
    run_campaign(path, res, env3.deps())
    assert env3.events == []


def test_na_written_with_reason(tmp_path):
    env = Env(tmp_path, unsupported={"alisql"})
    run_campaign(write_yaml(tmp_path), tmp_path / "res", env.deps())
    recs = records(tmp_path / "res", "alisql")
    assert len(recs) == 22
    assert {r.status for r in recs} == {"n/a"}
    assert {r.reason for r in recs} == {"not supported here"}
    assert all(r.value is None for r in recs)
    assert "alisql" not in env.started()


def test_mark_failed_writes_all_cells(tmp_path):
    path = write_yaml(tmp_path)
    res = tmp_path / "res"
    n = mark_failed(path, res, system="alisql", error="engine would not start",
                    repro="docker run dbench/alisql:8.0.44-3", host=lambda: HOST, clock=lambda: NOW)
    assert n == 22
    recs = records(res, "alisql")
    assert sorted(r.item for r in recs) == [f"q{i:02d}" for i in range(1, 23)]
    assert {r.status for r in recs} == {"failed"}
    assert {r.reason for r in recs} == {"engine would not start"}
    assert {r.extra["repro"] for r in recs} == {"docker run dbench/alisql:8.0.44-3"}
    assert all(r.measure == "M1" and r.run_id == "r1" and r.sf == 1 for r in recs)
    # an existing result is kept
    again = mark_failed(path, res, system="alisql", error="e", repro="r",
                        host=lambda: HOST, clock=lambda: NOW)
    assert again == 0


def test_mark_failed_unknown_system_and_run_id(tmp_path):
    path = write_yaml(tmp_path)
    with pytest.raises(CampaignError, match="not planned"):
        mark_failed(path, tmp_path / "res", system="mysql57", error="e", repro="r",
                    host=lambda: HOST)
    mark_failed(path, tmp_path / "res", system="alisql", error="e", repro="r", run_id="other",
                host=lambda: HOST, clock=lambda: NOW)
    assert exists(tmp_path / "res", "alisql", "M1", 1, "q01", "other")


def test_mark_failed_track_b_system(tmp_path):
    path = write_yaml(tmp_path, track_b={"enabled": True, "systems": ["villagesql"]})
    n = mark_failed(path, tmp_path / "res", system="villagesql", error="e", repro="r",
                    host=lambda: HOST, clock=lambda: NOW)
    assert n == 23  # q01..q22 plus the join item
    assert exists(tmp_path / "res", "villagesql", "M1", 1, "join_innodb", "r1-trackb")


def test_refuses_bad_limits(tmp_path):
    env = Env(tmp_path, host=(4, 62))  # fake docker info: 4 cpus; db_cpuset 0-7 exceeds it
    with pytest.raises(ValueError, match="exceeds host cores"):
        run_campaign(write_yaml(tmp_path), tmp_path / "res", env.deps())
    assert env.events == [] and not (tmp_path / "res").exists()

    env = Env(tmp_path, host=(20, 20))  # 16 GB + 16 GB reserve > 20 GB of docker RAM
    with pytest.raises(ValueError, match="exceeds host RAM"):
        run_campaign(write_yaml(tmp_path), tmp_path / "res", env.deps())
    assert env.events == []


def test_one_system_at_a_time(tmp_path):
    env = Env(tmp_path)
    run_campaign(write_yaml(tmp_path), tmp_path / "res", env.deps())
    order = [(k, n) for k, n in env.events if k in ("start", "stop")]
    # strictly start A, stop A, start B, stop B ...
    assert [k for k, _ in order] == ["start", "stop"] * 5  # reference + 4 systems
    for (_, a), (_, b) in zip(order[0::2], order[1::2], strict=True):
        assert a == b


def test_unregistered_measure_rejected_at_load(tmp_path):
    path = write_yaml(tmp_path, measures=["M1", "M99"])
    with pytest.raises(CampaignError, match="M99"):
        load_campaign(path)
    env = Env(tmp_path)
    with pytest.raises(CampaignError, match="M99"):
        run_campaign(path, tmp_path / "res", env.deps())
    assert env.events == []
    assert "M1" in measures.registered()


@pytest.mark.parametrize("over, msg", [
    ({"sf": 0}, "sf"),
    ({"systems": []}, "systems"),
    ({"systems": ["nope"]}, "unknown system"),
    ({"limits": {"db_cpuset": "0-7"}}, "limits"),
    ({"timeout_s": -1}, "timeout_s"),
    ({"bogus": 1}, "bogus"),
    ({"run_id": ""}, "run_id"),
    ({"systems": ["duckdb-1.5.5", "alisql"]}, "innodb"),
])
def test_load_validation_errors(tmp_path, over, msg):
    with pytest.raises(CampaignError, match=msg):
        load_campaign(write_yaml(tmp_path, **over))


def test_load_defaults_and_shapes(tmp_path):
    doc = {
        "run_id": "r", "sf": 100, "systems": ["duckdb-1.5.5", "alisql"], "measures": ["M1"],
        "limits": {"db_cpuset": "0-7", "runner_cpuset": "8-11", "mem_gb": 16},
        "reference": {"system": "duckdb-1.5.5", "cross_check": ["duckdb-1.4.3"]},
        "track_b": True,
    }
    path = tmp_path / "c.yaml"
    path.write_text(json.dumps(doc))
    c = load_campaign(path)
    assert isinstance(c, Campaign)
    assert c.timeout_s == 900 and c.limits == Limits("0-7", "8-11", 16)
    assert c.track_b_enabled and "villagesql" in c.track_b_systems


def test_digest_drift_refused(tmp_path):
    lock_path = tmp_path / "versions.lock.json"
    lock_path.write_text(json.dumps({"innodb": {
        "image": "mysql:9.7", "digest": "sha256:aaa", "digest_kind": "registry",
        "server_version": "9.7", "duckdb_version": None, "build_inputs": {}}}))
    with pytest.raises(lock.LockError, match="innodb.*digest"):
        lock.check_lock(["innodb"], lock_path,
                        identity=lambda image: ("registry", ("sha256:bbb",)))
    lock.check_lock(["innodb"], lock_path,
                    identity=lambda image: ("registry", ("sha256:zzz", "sha256:aaa")))

    # the campaign refuses before anything starts
    env = Env(tmp_path)
    deps = env.deps()
    deps.lock_check = lambda systems: lock.check_lock(
        systems, lock_path, identity=lambda image: ("registry", ("sha256:bbb",)))
    with pytest.raises(lock.LockError, match="innodb"):
        run_campaign(write_yaml(tmp_path), tmp_path / "res", deps)
    assert env.events == []


def test_local_image_id_drift_refused(tmp_path):
    inputs = lock.build_inputs("alisql")
    lock_path = tmp_path / "versions.lock.json"
    lock_path.write_text(json.dumps({"alisql": {
        "image": "dbench/alisql:8.0.44-3", "digest": "sha256:old", "digest_kind": "local",
        "server_version": "x", "duckdb_version": "y", "build_inputs": inputs}}))
    with pytest.raises(lock.LockError, match="alisql"):
        lock.check_lock(["alisql"], lock_path,
                        identity=lambda image: ("local", ("sha256:new",)))


def test_build_inputs_drift_refused(tmp_path):
    inputs = lock.build_inputs("alisql")
    assert inputs["args"]["ALISQL_SHA256"]  # parsed from the Dockerfile
    lock_path = tmp_path / "versions.lock.json"
    changed = {**inputs, "args": {**inputs["args"], "ALISQL_SHA256": "0" * 64}}
    lock_path.write_text(json.dumps({"alisql": {
        "image": "dbench/alisql:8.0.44-3", "digest": "sha256:same", "digest_kind": "local",
        "server_version": "x", "duckdb_version": "y", "build_inputs": changed}}))
    with pytest.raises(lock.LockError, match="alisql.*build inputs"):
        lock.check_lock(["alisql"], lock_path,
                        identity=lambda image: ("local", ("sha256:same",)))


def test_lock_missing_system_refused(tmp_path):
    lock_path = tmp_path / "versions.lock.json"
    lock_path.write_text("{}")
    with pytest.raises(lock.LockError, match="postgres.*dbench lock"):
        lock.check_lock(["postgres"], lock_path, identity=lambda i: ("registry", ("sha256:a",)))


def test_build_inputs_cover_locally_built_images():
    assert lock.build_inputs("innodb") == {}
    assert lock.build_inputs("mariadb")["args"]["MARIADB_VERSION"] == "13.0.2-1.el9"
    assert lock.build_inputs("villagesql")["args"]["EXT_COMMIT"]
    duck = lock.build_inputs("duckdb-1.5.5")
    assert duck["args"]["DUCKDB_VERSION"] == "1.5.5" and "@sha256:" in duck["from"][0]
    assert lock.build_inputs("pg_ducklake")["COMMIT"]
    # Track B variants share the Track A image and lock entry
    assert lock.lock_name("mariadb-trackb") == "mariadb"


def test_reference_disagreement_blocks(tmp_path):
    res = tmp_path / "res"
    env = Env(tmp_path, rows={"innodb": [(2,)]})  # reference rows are [(1,)]
    with pytest.raises(CampaignBlocked, match="innodb.*q01"):
        run_campaign(write_yaml(tmp_path), res, env.deps())
    bad = [r for r in records(res, "innodb") if r.status == "wrong_result"]
    assert bad and all(r.extra["reference_disagreement"] for r in bad)
    assert "alisql" not in env.started()  # stopped before the engines ran


def test_gate_systems_run_first_and_resume_still_blocks(tmp_path):
    res = tmp_path / "res"
    env = Env(tmp_path, rows={"postgres": [(2,)]})
    path = write_yaml(tmp_path)
    with pytest.raises(CampaignBlocked, match="postgres"):
        run_campaign(path, res, env.deps())
    assert env.started()[1:3] == ["innodb", "postgres"]
    env2 = Env(tmp_path, rows={"postgres": [(2,)]})
    with pytest.raises(CampaignBlocked, match="postgres"):  # cells exist; still blocked
        run_campaign(path, res, env2.deps())


def test_engine_wrong_result_does_not_block(tmp_path):
    env = Env(tmp_path, rows={"alisql": [(2,)]})
    run_campaign(write_yaml(tmp_path), tmp_path / "res", env.deps())
    recs = records(tmp_path / "res", "alisql")
    assert {r.status for r in recs} == {"wrong_result"}


def test_cross_check_disagreement_blocks_at_sf100(tmp_path):
    path = write_yaml(tmp_path, sf=100, systems=["duckdb-1.5.5", "alisql"])
    env = Env(tmp_path, rows={"duckdb-1.4.3": [(3,)]})
    with pytest.raises(CampaignBlocked, match=r"duckdb-1\.4\.3.*q01"):
        run_campaign(path, tmp_path / "res", env.deps())
    bad = records(tmp_path / "res", "duckdb-1.4.3")
    assert bad and bad[0].extra["reference_disagreement"]
    assert env.started() == ["duckdb-1.5.5", "duckdb-1.4.3"]


def test_cross_check_agreement_proceeds_at_sf100(tmp_path):
    path = write_yaml(tmp_path, sf=100, systems=["duckdb-1.5.5", "alisql"])
    env = Env(tmp_path)
    run_campaign(path, tmp_path / "res", env.deps())
    assert len(records(tmp_path / "res", "alisql")) == 22
    assert env.started() == ["duckdb-1.5.5", "duckdb-1.4.3", "duckdb-1.5.5", "alisql"]


def test_trackb_section_invokes_run_track_b(tmp_path):
    env = Env(tmp_path)
    path = write_yaml(
        tmp_path, track_b={"enabled": True, "systems": ["duckdb-1.5.5", "villagesql"]})
    run_campaign(path, tmp_path / "res", env.deps())
    (call,) = env.trackb_calls
    sf, run_id, systems, kw = call
    assert (sf, run_id, systems) == (1, "r1-trackb", ["duckdb-1.5.5", "villagesql"])
    assert kw["limits"] == Limits("0-7", "8-11", 16) and kw["timeout_s"] == 900
    # the lock covers the Track B systems too
    assert "villagesql" in env.lock_calls[0]


def test_trackb_disabled_not_invoked(tmp_path):
    env = Env(tmp_path)
    run_campaign(write_yaml(tmp_path), tmp_path / "res", env.deps())
    assert env.trackb_calls == []


def test_load_failure_recorded_as_failed(tmp_path):
    env = Env(tmp_path)
    factory = env.factory

    def broken(name, data_dir):
        a = factory(name, data_dir)
        if name == "alisql":
            a.start = lambda limits: (_ for _ in ()).throw(RuntimeError("boom"))
        return a

    deps = env.deps()
    deps.adapter_factory = broken
    run_campaign(write_yaml(tmp_path), tmp_path / "res", deps)
    recs = records(tmp_path / "res", "alisql")
    assert len(recs) == 22 and {r.status for r in recs} == {"failed"}
    assert all("boom" in r.reason for r in recs)
    assert len(records(tmp_path / "res", "innodb")) == 22  # the campaign went on


def test_cli_mark_failed(tmp_path, capsys, monkeypatch):
    from dbench import cli

    monkeypatch.setattr("dbench.hostinfo.host_info", lambda: HOST)
    path = write_yaml(tmp_path)
    rc = cli.main(["mark-failed", "--system", "alisql", "--error", "e", "--repro", "r",
                   "--campaign", str(path), "--results", str(tmp_path / "res")])
    assert rc == 0 and "22 cells" in capsys.readouterr().out
    rc = cli.main(["mark-failed", "--system", "nope", "--error", "e", "--repro", "r",
                   "--campaign", str(path), "--results", str(tmp_path / "res")])
    assert rc == 2
