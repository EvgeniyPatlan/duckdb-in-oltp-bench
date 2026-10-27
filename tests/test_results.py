from pathlib import Path

import pytest

from dbench import config
from dbench.results import ResultRecord, exists, iter_records, key, read, write


def make(**over):
    base = dict(
        system="duckdb", track="A", measure="M1", sf=1, item="q07",
        status="ok", value=1.5, unit="s", extra={"cold": 2.0}, error=None,
        reason=None, versions={"server": "1", "duckdb": "1.4", "image_digest": "sha256:x"},
        config_ref="systems/duckdb/conf", host={"cores": 20},
        run_id="r1", started_at="2026-10-27T00:00:00Z",
    )
    base.update(over)
    return ResultRecord(**base)


def test_limits_defaults():
    lim = config.Limits()
    assert (lim.db_cpuset, lim.runner_cpuset, lim.mem_gb) == ("0-15", "16-23", 64)
    assert config.QUERY_TIMEOUT_S == 900
    assert config.STREAMS == (1, 4, 8, 16, 32)
    assert config.WARM_RUNS == 3
    assert config.SCALE_FACTORS == (1, 10, 100, 300)


def test_roundtrip(tmp_path: Path):
    rec = make()
    p = write(rec, tmp_path)
    assert p == tmp_path / "r1" / "duckdb" / "M1-sf1-q07.json"
    assert read(p) == rec
    assert [r for r in iter_records(tmp_path)] == [rec]
    assert key(rec) == ("duckdb", "M1", 1, "q07", "r1")


def test_json_sorted_indent2(tmp_path: Path):
    text = write(make(), tmp_path).read_text(encoding="utf-8")
    assert text.startswith('{\n  "config_ref"')


@pytest.mark.parametrize("status", ["n/a", "failed"])
def test_requires_reason(status):
    with pytest.raises(ValueError):
        make(status=status, value=None, reason=None)
    assert make(status=status, value=None, reason="why").reason == "why"


@pytest.mark.parametrize(
    "over",
    [{"measure": "M0"}, {"measure": "M14"}, {"measure": "x"},
     {"track": "C"}, {"status": "weird"}],
)
def test_rejects_invalid(over):
    with pytest.raises(ValueError):
        make(**over)


def test_exists(tmp_path: Path):
    rec = make(item="streams=8")
    assert not exists(tmp_path, "duckdb", "M1", 1, "streams=8", "r1")
    write(rec, tmp_path)
    assert exists(tmp_path, "duckdb", "M1", 1, "streams=8", "r1")
    assert not exists(tmp_path, "duckdb", "M1", 10, "streams=8", "r1")


def test_item_sanitized_and_exists_consistent(tmp_path: Path):
    rec = make(item="a/b c:d")
    p = write(rec, tmp_path)
    assert p.name == "M1-sf1-a_b_c_d.json"
    assert p.parent == tmp_path / "r1" / "duckdb"
    assert exists(tmp_path, "duckdb", "M1", 1, "a/b c:d", "r1")
    assert read(p) == rec


def test_iter_skips_non_json_and_underscore_dirs(tmp_path: Path):
    write(make(), tmp_path)
    (tmp_path / "r1" / "notes.txt").write_text("x")
    ref = tmp_path / "_reference" / "duckdb"
    ref.mkdir(parents=True)
    (ref / "M1-sf1-q01.json").write_text("{}")
    assert len(list(iter_records(tmp_path))) == 1


def test_write_is_atomic(tmp_path: Path, monkeypatch):
    import json as _json

    def boom(obj, fh, **kw):
        fh.write('{"partial": ')
        raise RuntimeError("killed")

    monkeypatch.setattr(_json, "dump", boom)
    with pytest.raises(RuntimeError):
        write(make(), tmp_path)
    assert not exists(tmp_path, "duckdb", "M1", 1, "q07", "r1")
    assert not (tmp_path / "r1" / "duckdb" / "M1-sf1-q07.json").exists()
    assert list((tmp_path / "r1" / "duckdb").iterdir()) == []


def test_iter_ignores_tmp_files(tmp_path: Path):
    p = write(make(), tmp_path)
    (p.parent / "M1-sf1-q02.json.tmp").write_text('{"partial": ')
    assert len(list(iter_records(tmp_path))) == 1
