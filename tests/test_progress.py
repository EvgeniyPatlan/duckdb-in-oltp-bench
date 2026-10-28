"""Campaign progress lines: one per recorded cell, plus system start/load/end."""
from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

from dbench.campaign import run_campaign
from dbench.progress import Progress
from dbench.results import observe, write
from tests.test_campaign import NOW, Env, write_yaml
from tests.test_overhead import m1


class Clock:
    def __init__(self):
        self.now = NOW

    def __call__(self):
        return self.now


def test_record_line_has_time_system_counter_item_status_and_value():
    lines, clock = [], Clock()
    p = Progress(lines.append, clock)
    p.system_start("innodb", {"M1": 22, "M5": 120}, skipped=3)
    clock.now = NOW + timedelta(minutes=2)
    rec = replace(m1("innodb", "q05", 12.34, version="x", track="baseline"),
                  extra={"compared": True})
    p.record(rec)
    assert lines[0].endswith("[innodb] start: M1 22, M5 120 to run; 3 already recorded")
    assert lines[1] == "00:02:00 [innodb] M1 1/22 q05: ok 12.3 s (match)"


def test_failure_lines_carry_the_reason_and_mismatch_is_loud():
    lines = []
    p = Progress(lines.append, Clock())
    p.system_start("alisql", {"M1": 2}, skipped=0)
    p.record(replace(m1("alisql", "q01", None, version="x", status="timeout"),
                     error="query exceeded 900 s\nTraceback ..."))
    wrong = replace(m1("alisql", "q02", 1.0, version="x", status="wrong_result"),
                    extra={"compared": True})
    p.record(wrong)
    assert lines[1].endswith("M1 1/2 q01: timeout (query exceeded 900 s)")
    assert lines[2].endswith("M1 2/2 q02: wrong_result 1.0 s (MISMATCH)")


def test_system_end_summarises_statuses_and_elapsed():
    lines, clock = [], Clock()
    p = Progress(lines.append, clock)
    p.system_start("percona", {"M1": 2}, skipped=0)
    p.record(m1("percona", "q01", 1.0, version="x"))
    p.record(m1("percona", "q02", None, version="x", status="timeout"))
    clock.now = NOW + timedelta(hours=1, minutes=5)
    p.system_end("percona")
    assert lines[-1] == "01:05:00 [percona] done in 1h05m: ok 1, timeout 1"


def test_records_of_unknown_measures_still_print():
    lines = []
    p = Progress(lines.append, Clock())
    p.record(m1("postgres", "q01", 2.0, version="x"))  # no system_start (e.g. Track B)
    assert lines[0].endswith("[postgres] M1 q01: ok 2.0 s")


def test_observe_sees_every_write_and_a_broken_listener_loses_nothing(tmp_path):
    seen = []

    def boom(rec):
        raise RuntimeError("printer broke")

    with observe(seen.append), observe(boom):
        path = write(m1("alisql", "q01", 1.0, version="x"), tmp_path)
    write(m1("alisql", "q02", 1.0, version="x"), tmp_path)  # after the block: not observed
    assert path.exists() and [r.item for r in seen] == ["q01"]


def test_campaign_prints_progress(tmp_path):
    lines = []
    deps = replace(Env(tmp_path).deps(), out=lines.append)
    run_campaign(write_yaml(tmp_path), tmp_path / "res", deps)
    text = "\n".join(lines)
    assert "[innodb] start: M1 22 to run" in text
    assert "[innodb] loading SF1" in text
    assert "[innodb] M1 22/22 q22: ok" in text
    assert "[alisql] done in" in text
