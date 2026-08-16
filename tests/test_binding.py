import inspect
import json
import multiprocessing
import socket
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

import singbox


MINIMAL_CONFIG = json.dumps({"log": {"disabled": True}})
METRICS_CONFIG = json.dumps(
    {
        "log": {"disabled": True},
        "experimental": {"clash_api": {}},
    }
)
V2RAY_METRICS_CONFIG = json.dumps(
    {
        "log": {"disabled": True},
        "experimental": {
            "v2ray_api": {
                "listen": "127.0.0.1:0",
                "stats": {"enabled": True, "outbounds": ["direct"]},
            }
        },
    }
)
NAIVE_CONFIG = json.dumps(
    {
        "log": {"disabled": True},
        "outbounds": [
            {
                "type": "naive",
                "tag": "naive-test",
                "server": "127.0.0.1",
                "server_port": 443,
                "username": "test",
                "password": "test",
                "tls": {"enabled": True, "server_name": "example.com"},
            }
        ],
    }
)


def _join_or_terminate(process, timeout=20):
    process.join(timeout)
    if process.is_alive():
        process.terminate()
        process.join(10)
        pytest.fail("the sing-box child process did not exit in time")


def _listening_config(port):
    return json.dumps(
        {
            "log": {"disabled": True},
            "inbounds": [
                {
                    "type": "mixed",
                    "tag": "test-in",
                    "listen": "127.0.0.1",
                    "listen_port": port,
                }
            ],
        }
    )


def test_import_version_and_documentation():
    assert singbox.__version__ == "v1.13.18"
    assert issubclass(singbox.SingBoxError, RuntimeError)
    assert not hasattr(singbox, "start_from_json")
    assert "multiprocessing.Process" in inspect.getdoc(singbox.startFromJSON)
    assert "non-blocking" in inspect.getdoc(singbox.SingBox)
    assert inspect.getdoc(singbox.SingBox.running)
    assert inspect.getdoc(singbox.SingBox.handle)


@pytest.mark.parametrize(
    "config",
    ["{", json.dumps({"not_a_sing_box_option": True})],
)
def test_managed_invalid_configuration_is_a_controlled_error(config):
    core = singbox.SingBox()
    with pytest.raises(singbox.SingBoxError):
        core.startFromJSON(config)
    assert not core.running


def test_managed_valid_start_and_stop():
    core = singbox.SingBox()
    core.startFromJSON(MINIMAL_CONFIG)
    assert core.running
    assert core.handle > 0
    core.stop()
    assert not core.running
    core.stop()  # idempotent at the Python object boundary


def test_managed_repeated_lifecycle():
    core = singbox.SingBox()
    for _ in range(2):
        core.startFromJSON(MINIMAL_CONFIG)
        assert core.running
        core.stop()
        assert not core.running


def test_naive_outbound_initializes_cronet():
    with singbox.SingBox() as core:
        core.startFromJSON(NAIVE_CONFIG)
        assert core.running


def test_managed_context_manager():
    with singbox.SingBox() as core:
        core.startFromJSON(MINIMAL_CONFIG)
        assert core.running
    assert not core.running


def test_native_statistics_snapshot():
    with singbox.SingBox() as core:
        core.startFromJSON(METRICS_CONFIG)
        stats = core.queryStats()
    assert stats["runtime"]["goroutines"] > 0
    assert stats["runtime"]["sys"] > 0
    assert stats["clash"] == {
        "uplink_bytes": 0,
        "downlink_bytes": 0,
        "active_connections": 0,
    }
    assert stats["v2ray"] is None


def test_native_v2ray_statistics_snapshot():
    with singbox.SingBox() as core:
        core.startFromJSON(V2RAY_METRICS_CONFIG)
        stats = core.queryStats(patterns=["outbound"], reset=True)
    assert stats["clash"] is None
    assert stats["v2ray"] == {"counters": []}


def test_concurrent_queries_do_not_deadlock():
    with singbox.SingBox() as core:
        core.startFromJSON(METRICS_CONFIG)

        def query_once():
            try:
                return core.queryStats()["runtime"]["goroutines"]
            except RuntimeError:
                # Competing operations fail fast instead of waiting while a
                # Python thread owns the GIL.
                return None

        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = [executor.submit(query_once) for _ in range(100)]
            results = [future.result(timeout=5) for future in futures]
    assert any(result is not None for result in results)


def test_process_entry_point_uses_configuration_exit_code():
    process = multiprocessing.get_context("spawn").Process(
        target=singbox.startFromJSON,
        args=("{",),
    )
    process.start()
    _join_or_terminate(process)
    assert process.exitcode == 23


def test_process_entry_point_uses_distinct_startup_exit_code():
    with socket.socket() as occupied:
        occupied.bind(("127.0.0.1", 0))
        occupied.listen()
        config = _listening_config(occupied.getsockname()[1])
        process = multiprocessing.get_context("spawn").Process(
            target=singbox.startFromJSON,
            args=(config,),
        )
        process.start()
        _join_or_terminate(process)
    assert process.exitcode not in (0, 23)


def test_process_entry_point_starts_and_blocks_until_terminated():
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    process = multiprocessing.get_context("spawn").Process(
        target=singbox.startFromJSON,
        args=(_listening_config(port),),
    )
    process.start()

    deadline = time.monotonic() + 20
    started = False
    while process.is_alive() and time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                started = True
                break
        except OSError:
            time.sleep(0.05)
    if not started:
        _join_or_terminate(process, timeout=0)
        pytest.fail(f"sing-box did not start; child exit code: {process.exitcode}")

    process.terminate()
    process.join(20)
    if process.is_alive():
        process.kill()
        process.join(10)
        pytest.fail("the running sing-box child did not terminate")
    assert process.exitcode is not None
