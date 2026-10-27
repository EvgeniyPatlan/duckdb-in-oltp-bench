import json
import os
from pathlib import Path

import pytest

from dbench import datagen
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


def test_part_counts_split_only_large_tables():
    assert datagen.part_count("lineitem", 1) == 1
    assert datagen.part_count("lineitem", 10) == 4
    assert datagen.part_count("lineitem", 100) > 30
    assert datagen.part_count("region", 300) == 1
    assert datagen.part_count("lineitem", 100) * datagen.PART_BYTES >= 100 * 725 * 10**6


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


@pytest.mark.docker
@pytest.mark.skipif(os.environ.get("DBENCH_DOCKER") != "1", reason="set DBENCH_DOCKER=1")
def test_generate_sf_0_01():
    import shutil

    out = datagen.data_root() / "tpch-test"
    shutil.rmtree(out, ignore_errors=True)
    try:
        paths = datagen.generate(0.01, out)
        assert paths.root == out / "sf0.01"
        assert set(paths.tables) == set(datagen.TABLES) and len(paths.tables) == 8
        assert set(paths.parquet_tables) == set(datagen.TABLES)
        assert datagen.verify(paths)
        rows = {
            f["table"]: 0 for f in json.loads(paths.manifest.read_text())["files"]
        }
        for f in json.loads(paths.manifest.read_text())["files"]:
            if f["format"] == "csv":
                rows[f["table"]] += f["rows"]
        assert rows["lineitem"] == 60175
        first = paths.tables["lineitem"][0].read_text().splitlines()[0]
        assert "|" in first and not first.endswith("|")
        again = datagen.generate(0.01, out)
        assert again == paths
    finally:
        shutil.rmtree(out, ignore_errors=True)
