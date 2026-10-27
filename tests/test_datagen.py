import json
import math
import os
import shutil
from pathlib import Path

import pytest

from dbench import datagen, dockerctl
from dbench.datagen import DatasetPaths


def _fake_dataset(root: Path) -> DatasetPaths:
    files = []
    for fmt, ext in (("csv", "csv"), ("parquet", "parquet")):
        for table, parts in (("region", 1), ("lineitem", 2)):
            for k in range(parts):
                p = root / fmt / table / f"{table}.{k}.{ext}"
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(f"{fmt}-{table}-{k}".encode())
                files.append((p, table, fmt, 5 + k))
    return datagen.write_manifest(root, 1, "1.5.5", files)


def test_manifest_roundtrip(tmp_path):
    paths = _fake_dataset(tmp_path / "sf1")
    assert paths.manifest == tmp_path / "sf1" / "manifest.json"
    data = json.loads(paths.manifest.read_text())
    assert data["sf"] == 1 and data["duckdb_version"] == "1.5.5" and data["generated_at"]
    entry = next(f for f in data["files"] if f["path"] == "csv/lineitem/lineitem.1.csv")
    assert entry["rows"] == 6 and entry["table"] == "lineitem" and entry["format"] == "csv"
    assert entry["bytes"] == len(b"csv-lineitem-1") and len(entry["sha256"]) == 64
    loaded = datagen.load_paths(paths.root)
    assert loaded == paths
    assert loaded.tables["lineitem"] == [
        paths.csv_dir / "lineitem" / "lineitem.0.csv",
        paths.csv_dir / "lineitem" / "lineitem.1.csv",
    ]
    assert set(loaded.parquet_tables) == {"region", "lineitem"}
    assert datagen.verify(loaded)


def test_part_order_is_numeric(tmp_path):
    root = tmp_path / "sf1"
    files = []
    for k in (10, 2, 0):
        p = root / "csv" / "t" / f"t.{k}.csv"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x")
        files.append((p, "t", "csv", 1))
    paths = datagen.write_manifest(root, 1, "1.5.5", files)
    assert [p.name for p in paths.tables["t"]] == ["t.0.csv", "t.2.csv", "t.10.csv"]


@pytest.mark.parametrize("victim", ["csv/lineitem/lineitem.1.csv",
                                    "parquet/region/region.0.parquet"])
def test_verify_detects_tamper(tmp_path, victim):
    paths = _fake_dataset(tmp_path / "sf1")
    assert datagen.verify(paths)
    target = paths.root / victim
    target.write_bytes(b"tampered")
    assert not datagen.verify(paths)


def test_verify_detects_missing_file_and_manifest(tmp_path):
    paths = _fake_dataset(tmp_path / "sf1")
    (paths.root / "csv/region/region.0.csv").unlink()
    assert not datagen.verify(paths)
    paths.manifest.unlink()
    assert not datagen.verify(paths)


def test_dataset_dir_names():
    assert datagen.dataset_dir_name(0.01) == "sf0.01"
    assert datagen.dataset_dir_name(0.1) == "sf0.1"
    assert datagen.dataset_dir_name(1) == "sf1"
    assert datagen.dataset_dir_name(1.0) == "sf1"
    assert datagen.dataset_dir_name(100) == "sf100"


def test_part_counts_split_only_from_sf100():
    for sf in (0.01, 1, 10):
        assert all(datagen.part_count(t, sf) == 1 for t in datagen.TABLES)
    assert datagen.part_count("lineitem", 100) > 1
    assert datagen.part_count("region", 300) == 1
    assert datagen.part_count("lineitem", 0.01, part_bytes=3_000_000) == 3


@pytest.mark.parametrize("sf", [100, 300])
def test_part_size_estimate_fits_target(sf):
    for table, per_sf in datagen.CSV_BYTES_PER_SF.items():
        parts = datagen.part_count(table, sf)
        assert math.ceil(per_sf * sf / parts) <= 1.8e9
    assert datagen.PART_TARGET_BYTES <= 1.8e9 and datagen.PART_BYTES == 2e9
    # calibrated from measured SF1 files (lineitem 771865717, orders 173452161)
    assert datagen.CSV_BYTES_PER_SF["lineitem"] >= 771_865_717
    assert datagen.CSV_BYTES_PER_SF["orders"] >= 173_452_161


def test_generate_fails_without_manifest_when_a_part_is_too_big(tmp_path, monkeypatch):
    monkeypatch.setattr(datagen, "PART_BYTES", 5)

    def fake(sf, work, image, part_bytes):
        p = work / "csv" / "region" / "region.0.csv"
        p.parent.mkdir(parents=True)
        p.write_bytes(b"0123456789")
        return {"duckdb_version": "1.5.5",
                "files": [{"path": "/out/csv/region/region.0.csv", "table": "region",
                           "format": "csv", "rows": 1}]}

    monkeypatch.setattr(datagen, "_run_generator", fake)
    with pytest.raises(RuntimeError, match="exceeds"):
        datagen.generate(100, tmp_path)
    assert not (tmp_path / "sf100").exists()
    assert not list(tmp_path.glob(".sf100*"))


def test_host_path_maps_dev_mount_and_leaves_external_roots(monkeypatch, tmp_path):
    monkeypatch.setenv("DBENCH_HOST_REPO", "/home/me/repo")
    assert dockerctl.host_path("/w/data/tpch") == "/home/me/repo/data/tpch"
    ext = tmp_path / "bulk" / "tpch"
    ext.mkdir(parents=True)
    assert dockerctl.host_path(ext) == str(ext.resolve())
    monkeypatch.delenv("DBENCH_HOST_REPO")
    assert dockerctl.host_path("/w/data") == "/w/data"


def test_data_root_env(monkeypatch, tmp_path):
    monkeypatch.setenv("DBENCH_DATA", str(tmp_path))
    assert datagen.data_root() == tmp_path
    monkeypatch.delenv("DBENCH_DATA")
    assert datagen.data_root() == Path(datagen.__file__).resolve().parents[1] / "data"


def test_generate_is_idempotent_when_verified(tmp_path, monkeypatch):
    root = tmp_path / "sf1"
    _fake_dataset(root)

    def boom(*a, **k):
        raise AssertionError("must not regenerate")

    monkeypatch.setattr(datagen, "_run_generator", boom)
    paths = datagen.generate(1, tmp_path)
    assert paths.root == root and paths.sf == 1


def _csv_lines(paths: DatasetPaths, table: str) -> int:
    return sum(p.read_bytes().count(b"\n") for p in paths.tables[table])


@pytest.fixture(scope="module")
def sf001():
    out = datagen.data_root() / "tpch-test"
    shutil.rmtree(out, ignore_errors=True)
    try:
        single = datagen.generate(0.01, out / "single")
        multi = datagen.generate(0.01, out / "multi", part_bytes=3_000_000)
        yield single, multi
    finally:
        shutil.rmtree(out, ignore_errors=True)


@pytest.mark.docker
@pytest.mark.skipif(os.environ.get("DBENCH_DOCKER") != "1", reason="set DBENCH_DOCKER=1")
def test_generate_sf_0_01(sf001):
    paths, _ = sf001
    assert paths.root.name == "sf0.01"
    assert set(paths.tables) == set(datagen.TABLES) and len(paths.tables) == 8
    assert set(paths.parquet_tables) == set(datagen.TABLES)
    assert datagen.verify(paths)
    assert _csv_lines(paths, "lineitem") == 60175
    assert _csv_lines(paths, "region") == 5 and _csv_lines(paths, "nation") == 25
    manifest_rows = {}
    for f in json.loads(paths.manifest.read_text())["files"]:
        if f["format"] == "csv":
            manifest_rows[f["table"]] = manifest_rows.get(f["table"], 0) + f["rows"]
    assert manifest_rows["lineitem"] == 60175
    first = paths.tables["lineitem"][0].read_text().splitlines()[0]
    assert "|" in first and not first.endswith("|")
    assert datagen.generate(0.01, paths.root.parent) == paths


@pytest.mark.docker
@pytest.mark.skipif(os.environ.get("DBENCH_DOCKER") != "1", reason="set DBENCH_DOCKER=1")
def test_generate_multi_part_matches_single_part(sf001):
    single, multi = sf001
    assert len(multi.tables["lineitem"]) == 3 and len(single.tables["lineitem"]) == 1
    assert len(multi.parquet_tables["lineitem"]) == 3
    assert _csv_lines(multi, "lineitem") == 60175
    assert datagen.verify(multi)
    for table in datagen.TABLES:
        joined = b"".join(p.read_bytes() for p in multi.tables[table])
        assert joined == b"".join(p.read_bytes() for p in single.tables[table]), table
