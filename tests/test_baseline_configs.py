
from dbench.config import Limits
from dbench.systems.baseline_conf import (
    innodb_settings,
    postgres_settings,
    render_innodb_cnf,
    render_postgres_conf,
)

PLAN = Limits("0-15", "16-23", 64)


def test_innodb_plan_values():
    text = render_innodb_cnf(PLAN)
    assert "innodb_buffer_pool_size = 48G" in text
    assert "innodb_parallel_read_threads = 16" in text
    assert "secure_file_priv = /data" in text
    assert "local_infile = OFF" in text
    assert "max_connections = 200" in text
    for line in ("join_buffer_size = 256M", "sort_buffer_size = 256M", "tmp_table_size = 4G",
                 "max_heap_table_size = 4G", "temptable_max_ram = 4G",
                 "innodb_redo_log_capacity = 8G"):
        assert line in text, line
    assert "{{" not in text


def test_postgres_plan_values():
    text = render_postgres_conf(PLAN)
    for line in ("shared_buffers = 16GB", "effective_cache_size = 48GB",
                 "max_parallel_workers_per_gather = 16", "max_parallel_workers = 16",
                 "max_worker_processes = 16", "work_mem = 256MB", "max_connections = 200",
                 "max_wal_size = 16GB", "checkpoint_timeout = 30min"):
        assert line in text, line
    assert "{{" not in text


def test_every_setting_has_a_reason_comment():
    for text in (render_innodb_cnf(PLAN), render_postgres_conf(PLAN)):
        lines = text.splitlines()
        for i, ln in enumerate(lines):
            if "=" in ln and not ln.lstrip().startswith(("#", "[")):
                assert lines[i - 1].lstrip().startswith("#"), ln


def test_sizing_scales_with_limits():
    small = Limits("0-7", "8-11", 16)
    assert innodb_settings(small)["buffer_pool"] == "12G"
    assert innodb_settings(small)["read_threads"] == 8
    assert innodb_settings(small)["temp_ram"] == "1G"
    assert innodb_settings(small)["redo_capacity"] == "2G"
    pg = postgres_settings(small)
    assert (pg["shared_buffers"], pg["effective_cache_size"], pg["workers"]) == ("4GB", "12GB", 8)
    assert pg["max_wal_size"] == "4GB"


def test_fractional_sizes_use_megabytes():
    assert innodb_settings(Limits("0-1", "2-3", 6))["buffer_pool"] == "4608M"
    assert postgres_settings(Limits("0-1", "2-3", 6))["shared_buffers"] == "1536MB"
