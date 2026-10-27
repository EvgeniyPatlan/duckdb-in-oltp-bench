from dbench.config import Limits
from dbench.systems import baseline_conf
from dbench.systems.alisql import render_config as alisql_cnf
from dbench.systems.mariadb import render_config as mariadb_cnf
from dbench.systems.percona import container_command, container_env

PLAN = Limits("0-15", "16-23", 64)


def kv(text: str) -> dict[str, str]:
    out = {}
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith(("#", "[")):
            k, _, v = line.partition("=")
            out[k.strip().lstrip("-")] = v.strip()
    return out


def configs() -> dict[str, dict[str, str]]:
    percona = kv("\n".join(container_command(PLAN)))
    return {"alisql": kv(alisql_cnf(PLAN)), "mariadb": kv(mariadb_cnf(PLAN)), "percona": percona}


def test_shared_formulas_render_identically_at_plan_limits():
    cfg = configs()
    for name, got in cfg.items():
        assert got["innodb_buffer_pool_size"] == "4G", name
        assert got["tmp_table_size"] == got["max_heap_table_size"] == "4G", name
        assert got["join_buffer_size"] == got["sort_buffer_size"] == "256M", name
        assert got["secure_file_priv"] == "/data" and got["max_connections"] == "200", name
    assert cfg["alisql"]["temptable_max_ram"] == cfg["percona"]["temptable_max_ram"] == "4G"
    redo = {"alisql": "innodb_redo_log_capacity", "percona": "innodb_redo_log_capacity",
            "mariadb": "innodb_log_file_size"}
    assert {n: cfg[n][k] for n, k in redo.items()} == {n: "8G" for n in redo}
    assert cfg["alisql"]["innodb_parallel_read_threads"] == \
        cfg["percona"]["innodb_parallel_read_threads"] == "16"
    assert cfg["alisql"]["duckdb_memory_limit"] == cfg["mariadb"]["duckdb_memory_limit"] == "48G"
    assert container_env(PLAN)["DUCKSDB_MEMORY_LIMIT"] == "48GB"
    assert cfg["alisql"]["duckdb_threads"] == cfg["mariadb"]["duckdb_max_threads"] == "16"
    assert (cfg["alisql"]["duckdb_temp_directory"] == cfg["mariadb"]["duckdb_temp_directory"]
            == container_env(PLAN)["DUCKSDB_TEMP_DIR"] == "/var/lib/mysql/duckdb_tmp")


def test_one_shared_settings_function():
    s = baseline_conf.duckdb_engine_settings(PLAN, "/x")
    assert (s["duck_memory"], s["cores"], s["buffer_pool"], s["temp_ram"], s["redo_capacity"],
            s["spill_dir"]) == ("48G", 16, "4G", "4G", "8G", "/x")


def test_binary_logging_is_off_in_every_mysql_family_config():
    texts = {"alisql": alisql_cnf(PLAN), "mariadb": mariadb_cnf(PLAN),
             "innodb": baseline_conf.render_innodb_cnf(PLAN),
             "percona": "\n".join(container_command(PLAN))}
    for name, text in texts.items():
        assert "skip-log-bin" in {k for k in kv(text)}, name
    for name in ("alisql", "mariadb", "innodb"):
        lines = texts[name].splitlines()
        i = lines.index("skip-log-bin")
        assert lines[i - 1].startswith("#"), name  # reason comment
