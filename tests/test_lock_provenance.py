"""Every record names the version lock it ran against (``versions.lock``)."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from dbench import cli, lock
from dbench.campaign import mark_failed
from dbench.measures.overhead import _derived
from dbench.results import ResultRecord, iter_records, read, stamp_lock, write

LOCAL_ID = "sha256:" + "a" * 64
REGISTRY = "sha256:" + "b" * 64


def make(system="percona", **over) -> ResultRecord:
    base = dict(system=system, track="A", measure="M1", sf=1, item="q01", status="ok",
                value=1.0, unit="s", config_ref="systems/percona/", run_id="r1",
                started_at="2026-11-05T00:00:00+00:00",
                versions={"image": "dbench/percona:x", "image_digest": LOCAL_ID})
    base.update(over)
    return ResultRecord(**base)


def lock_file(tmp_path: Path) -> Path:
    doc = {"percona": {"image": "dbench/percona:x", "digest": LOCAL_ID, "digest_kind": "local"},
           "postgres": {"image": "postgres:18", "digest": REGISTRY, "digest_kind": "registry"}}
    path = tmp_path / "versions.lock.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


def test_records_written_inside_stamp_lock_name_the_lock(tmp_path):
    with stamp_lock(lambda system: {"sha256": "f00", "system": system}):
        write(make(), tmp_path)
    rec = read(tmp_path / "r1" / "percona" / "M1-sf1-q01.json")
    assert rec.versions["lock"] == {"sha256": "f00", "system": "percona"}
    assert rec.versions["image_digest"] == LOCAL_ID


def test_without_stamp_lock_records_are_unchanged(tmp_path):
    rec = make()
    write(rec, tmp_path)
    assert read(tmp_path / "r1" / "percona" / "M1-sf1-q01.json") == rec


def test_a_record_keeps_its_own_lock_and_a_failing_provenance_is_recorded(tmp_path):
    def boom(system):
        raise OSError("no lock")

    with stamp_lock(boom):
        write(make(versions={"lock": {"sha256": "old"}}), tmp_path)
        write(make(item="q02"), tmp_path)
        write(make(item="q03", versions={"lock": None}), tmp_path)  # a rewrite of an old record
    by_item = {r.item: r for r in iter_records(tmp_path)}
    assert by_item["q01"].versions["lock"] == {"sha256": "old"}
    assert by_item["q02"].versions["lock"] == {"error": "OSError: no lock"}
    assert by_item["q03"].versions["lock"] is None


def test_provenance_names_sha_archive_and_locked_digest(tmp_path):
    path = lock_file(tmp_path)
    got = lock.provenance("percona", path)
    sha = lock.lock_sha256(path)
    assert got["sha256"] == sha and len(sha) == 64
    assert got["archived_as"] == f"_lock/versions.lock.{sha[:12]}.json"
    assert (got["digest"], got["digest_kind"]) == (LOCAL_ID, "local")
    assert {"git_commit", "lock_is_committed", "harness_dirty"} <= got.keys()
    # Track B variants run the Track A image; unknown systems carry no digest
    assert lock.provenance("percona-trackb", path)["digest"] == LOCAL_ID
    assert "digest" not in lock.provenance("nonesuch", path)


def test_archive_copies_the_lock_once(tmp_path):
    path = lock_file(tmp_path)
    first = lock.archive(tmp_path / "run", path)
    assert first.name == lock.archived_name(lock.lock_sha256(path))
    assert first.read_bytes() == path.read_bytes()
    assert lock.archive(tmp_path / "run", path) == first
    assert [p.name for p in first.parent.iterdir()] == [first.name]


def test_manifest_checks_digests_without_editing_records(tmp_path):
    path = lock_file(tmp_path)
    run = tmp_path / "results" / "r1"
    write(make(), run.parent)
    write(make(item="q02", versions={"image_digest": "sha256:" + "c" * 64}), run.parent)
    write(make(system="postgres", versions={"image_digest": f"postgres@{REGISTRY}"}), run.parent)
    write(make(system="duckdb-1.5.5", versions={"duckdb": "v1.5.5"}), run.parent)
    before = sorted(p.read_bytes() for p in run.rglob("*.json") if "_lock" not in p.parts)

    doc = lock.manifest(run, path)

    assert doc["systems"]["percona"] == {"match": 1, "differ": ["sha256:" + "c" * 64],
                                         "no_digest": 0}
    assert doc["systems"]["postgres"] == {"match": 1, "differ": [], "no_digest": 0}
    assert doc["systems"]["duckdb-1.5.5"]["no_digest"] == 1
    sha = lock.lock_sha256(path)
    assert (run / "_lock" / lock.archived_name(sha)).is_file()
    assert json.loads((run / "_lock" / f"manifest.{sha[:12]}.json").read_text()) == doc
    assert sorted(p.read_bytes() for p in run.rglob("*.json") if "_lock" not in p.parts) == before
    assert len(list(iter_records(run))) == 4  # the archive is not read as records


def test_m7_keeps_the_m1_lock_and_never_takes_the_current_one():
    old = _derived(make(), "overhead", "ok", 1.0, None, {})
    assert old.versions["lock"] is None
    new = _derived(make(versions={"lock": {"sha256": "m1"}}), "overhead", "ok", 1.0, None, {})
    assert new.versions["lock"] == {"sha256": "m1"}


def test_only_measuring_commands_stamp(monkeypatch):
    seen = {}

    def dispatch(args):
        from dbench import results
        seen[args.command] = bool(results._stampers)
        return 0

    monkeypatch.setattr(cli, "_dispatch", dispatch)
    cli.main(["overhead", "r1"])
    cli.main(["lock-manifest", "r1"])
    cli.main(["campaign", "campaigns/talk-sf100.yaml"])
    assert seen == {"overhead": False, "lock-manifest": False, "campaign": True}


def fake_git(answers):
    """``lock._git`` stand-in: the first word of the git subcommand -> (returncode, stdout)."""
    def run(*args):
        code, out = answers[args[0]]
        if isinstance(code, Exception):
            raise code
        return subprocess.CompletedProcess(args, code, out, "")
    return run


@pytest.mark.parametrize("answers, expected", [
    ({"rev-parse": (0, "abc\n"), "ls-files": (0, ""), "diff": (0, ""), "status": (0, "")},
     ("abc", True, False)),
    ({"rev-parse": (0, "abc\n"), "ls-files": (0, ""), "diff": (1, ""),
      "status": (0, " M dbench/cli.py\n")}, ("abc", False, True)),
    # an untracked lock is not the committed one, whatever diff says
    ({"rev-parse": (0, "abc\n"), "ls-files": (1, ""), "diff": (0, ""), "status": (0, "")},
     ("abc", False, False)),
    ({"rev-parse": (128, "")}, ("unknown", None, None)),  # no checkout / git dir not mounted
    ({"rev-parse": (OSError("no git"), "")}, ("unknown", None, None)),
    ({"rev-parse": (subprocess.TimeoutExpired("git", 30), "")}, ("unknown", None, None)),
])
def test_git_state(monkeypatch, tmp_path, answers, expected):
    monkeypatch.setattr(lock, "_git", fake_git(answers))
    lock._git_state.cache_clear()
    try:
        assert lock._git_state(lock_file(tmp_path)) == expected
    finally:
        lock._git_state.cache_clear()


def test_archive_and_provenance_share_the_first_read(tmp_path):
    path = lock_file(tmp_path)
    sha = lock.provenance("percona", path)["sha256"]
    path.write_text("{}", encoding="utf-8")  # rewritten after the process read it
    archived = lock.archive(tmp_path / "run", path)
    assert archived.name == lock.archived_name(sha)


def test_cli_archive_failure_does_not_stop_the_measurement(monkeypatch, tmp_path, capsys):
    def boom(run_dir):
        raise OSError("read-only")

    monkeypatch.setattr(lock, "archive", boom)
    cli._archive_lock(tmp_path / "r1")
    assert "could not archive the version lock" in capsys.readouterr().out


def test_mark_failed_archives_the_lock(tmp_path):
    from tests.test_campaign import HOST, NOW, write_yaml

    archived = []
    mark_failed(write_yaml(tmp_path), tmp_path / "res", system="alisql", error="e", repro="r",
                host=lambda: HOST, clock=lambda: NOW, archive_lock=archived.append)
    assert archived == [tmp_path / "res" / "r1"]


def test_campaign_archives_the_lock(tmp_path):
    from dbench.campaign import run_campaign
    from tests.test_campaign import Env, write_yaml

    env, archived = Env(tmp_path), []
    deps = env.deps()
    deps.archive_lock = archived.append
    run_campaign(write_yaml(tmp_path), tmp_path / "res", deps)
    assert archived == [tmp_path / "res" / "r1"]
