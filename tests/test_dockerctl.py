import os

import pytest

from dbench import dockerctl
from dbench.config import Limits


def test_overlapping_cpusets_rejected():
    with pytest.raises(ValueError, match="overlap"):
        dockerctl.validate_limits(Limits("0-15", "8-23"), host_cpus=80, host_mem_gb=512)


def test_out_of_range_rejected():
    with pytest.raises(ValueError, match="host"):
        dockerctl.validate_limits(Limits("0-95", "96-99"), host_cpus=80, host_mem_gb=512)


def test_runner_out_of_range_rejected():
    with pytest.raises(ValueError, match="host"):
        dockerctl.validate_limits(Limits("0-15", "16-80"), host_cpus=80, host_mem_gb=512)


def test_mem_too_large_rejected():
    with pytest.raises(ValueError, match="mem"):
        dockerctl.validate_limits(Limits(mem_gb=170), host_cpus=80, host_mem_gb=180)


def test_valid_limits_accepted():
    dockerctl.validate_limits(Limits(), host_cpus=80, host_mem_gb=180)
    dockerctl.validate_limits(Limits("0-15", "16-23", 64), host_cpus=24, host_mem_gb=80)


def test_mem_boundary_accepted():
    dockerctl.validate_limits(Limits(mem_gb=164), host_cpus=80, host_mem_gb=180)


@pytest.mark.parametrize("bad", ["", "a-b", "5-2", "0,,2", "-3", "1-2-3"])
def test_malformed_cpuset_rejected(bad):
    with pytest.raises(ValueError):
        dockerctl.validate_limits(Limits(bad, "16-23"), host_cpus=80, host_mem_gb=512)


def test_parse_cpuset_list_syntax():
    assert dockerctl.parse_cpuset("0,2,4-7") == {0, 2, 4, 5, 6, 7}


def test_host_mem_read_from_proc(monkeypatch):
    monkeypatch.setattr(dockerctl, "read_host_mem_gb", lambda: 20)
    with pytest.raises(ValueError, match="mem"):
        dockerctl.validate_limits(Limits(mem_gb=64), host_cpus=80)


def test_run_args_db():
    a = dockerctl.run_args(Limits(), "db")
    assert a["cpuset_cpus"] == "0-15"
    assert a["mem_limit"] == "64g"
    assert a["memswap_limit"] == "64g"
    assert "shm_size" not in a


def test_run_args_db_postgres():
    assert dockerctl.run_args(Limits(), "db", postgres=True)["shm_size"] == "8g"


def test_run_args_runner():
    a = dockerctl.run_args(Limits(), "runner")
    assert a["cpuset_cpus"] == "16-23"
    assert "mem_limit" not in a and "memswap_limit" not in a


def test_run_args_bad_role():
    with pytest.raises(ValueError):
        dockerctl.run_args(Limits(), "other")  # type: ignore[arg-type]


def test_wait_ready_times_out():
    with pytest.raises(TimeoutError):
        dockerctl.wait_ready(lambda: False, timeout_s=0.2, interval_s=0.05)


def test_wait_ready_tolerates_probe_errors():
    calls = []

    def probe():
        calls.append(1)
        if len(calls) < 3:
            raise ConnectionError("not yet")
        return True

    dockerctl.wait_ready(probe, timeout_s=5, interval_s=0.01)
    assert len(calls) == 3


docker = pytest.mark.skipif(
    os.environ.get("DBENCH_DOCKER") != "1", reason="set DBENCH_DOCKER=1"
)


@pytest.mark.docker
@docker
def test_start_stop_alpine():
    name = "dbench-test-alpine"
    limits = Limits(db_cpuset="0-7", runner_cpuset="8-11", mem_gb=1)
    c = dockerctl.start("alpine:3.20", name, limits, env={"X": "1"}, volumes={}, ports={},
                        command=["sleep", "60"])
    try:
        c.reload()
        hc = c.attrs["HostConfig"]
        assert hc["CpusetCpus"] == "0-7"
        assert hc["Memory"] == 1 << 30
        assert hc["MemorySwap"] == 1 << 30
        assert c.labels["dbench"] == "1"
        # stale container with same name is replaced
        c2 = dockerctl.start("alpine:3.20", name, limits, env={}, volumes={}, ports={},
                             command=["sleep", "60"])
        assert c2.id != c.id
        assert dockerctl.container_state(name)["status"] == "running"
    finally:
        dockerctl.stop(name)
    dockerctl.stop(name)  # no-op when absent


@pytest.mark.docker
@docker
def test_container_state_exit_and_logs():
    name = "dbench-test-state"
    limits = Limits(db_cpuset="0-7", runner_cpuset="8-11", mem_gb=1)
    dockerctl.start("alpine:3.20", name, limits, env={}, volumes={}, ports={},
                    command=["sh", "-c", "echo hello; exit 3"])
    try:
        import time
        time.sleep(2)
        st = dockerctl.container_state(name)
        assert st["status"] == "exited" and st["ExitCode"] == 3
        assert st["OOMKilled"] is False
        assert "hello" in st["logs"]
    finally:
        dockerctl.stop(name)


@pytest.mark.docker
@docker
def test_drop_caches():
    dockerctl.drop_caches()
