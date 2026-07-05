from pathlib import Path

import pytest

from canoniq.config import Config
from canoniq.ingest.dbt_manifest import DbtManifestConnector
from canoniq.ingest.query_log import QueryLogFileConnector
from canoniq.ingest.watcher import RawSignal, SignalWatcher

MANIFEST_FIXTURE = Path(__file__).parent / "fixtures" / "sample_manifest.json"


@pytest.fixture
def config() -> Config:
    return Config(
        project_name="test",
        warehouse_type="duckdb",
        output_formats=["metricflow"],
        output_dir="./out",
        poll_interval_seconds=1,
    )


@pytest.fixture
def query_log_file(tmp_path: Path) -> Path:
    log_file = tmp_path / "queries.sql"
    log_file.write_text("SELECT SUM(x) FROM t; SELECT COUNT(*) FROM t;")
    return log_file


def test_run_once_returns_query_log_signals(config: Config, query_log_file: Path) -> None:
    connector = QueryLogFileConnector(str(query_log_file))
    watcher = SignalWatcher(config, [connector])

    signals = watcher.run_once()

    assert len(signals) == 2
    assert all(isinstance(s, RawSignal) for s in signals)
    assert all(s.signal_type == "query_log" for s in signals)


def test_run_once_deduplicates_across_calls(config: Config, query_log_file: Path) -> None:
    connector = QueryLogFileConnector(str(query_log_file))
    watcher = SignalWatcher(config, [connector])

    first_pass = watcher.run_once()
    second_pass = watcher.run_once()

    assert len(first_pass) == 2
    assert second_pass == []  # nothing new since the file hasn't changed


def test_run_once_picks_up_new_query_after_dedup(config: Config, query_log_file: Path) -> None:
    connector = QueryLogFileConnector(str(query_log_file))
    watcher = SignalWatcher(config, [connector])

    watcher.run_once()
    query_log_file.write_text(
        "SELECT SUM(x) FROM t; SELECT COUNT(*) FROM t; SELECT AVG(y) FROM t;"
    )
    second_pass = watcher.run_once()

    assert len(second_pass) == 1
    assert "AVG(y)" in second_pass[0].payload.sql


def test_run_once_returns_dbt_manifest_signals(config: Config) -> None:
    connector = DbtManifestConnector(str(MANIFEST_FIXTURE))
    watcher = SignalWatcher(config, [connector])

    signals = watcher.run_once()

    assert len(signals) == 1
    assert signals[0].signal_type == "dbt_manifest"
    assert signals[0].payload["name"] == "total_net_profit"


def test_run_once_combines_multiple_connectors(
    config: Config, query_log_file: Path
) -> None:
    watcher = SignalWatcher(
        config,
        [QueryLogFileConnector(str(query_log_file)), DbtManifestConnector(str(MANIFEST_FIXTURE))],
    )

    signals = watcher.run_once()

    assert {s.signal_type for s in signals} == {"query_log", "dbt_manifest"}


def test_run_once_handles_unreadable_source_gracefully(config: Config, tmp_path: Path) -> None:
    connector = QueryLogFileConnector(str(tmp_path / "does_not_exist.sql"))
    watcher = SignalWatcher(config, [connector])

    signals = watcher.run_once()

    assert signals == []


def test_run_forever_polls_and_sleeps(
    config: Config, query_log_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    connector = QueryLogFileConnector(str(query_log_file))
    watcher = SignalWatcher(config, [connector])

    sleep_calls = []

    def fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)
        if len(sleep_calls) >= 2:
            raise KeyboardInterrupt

    monkeypatch.setattr("canoniq.ingest.watcher.time.sleep", fake_sleep)

    received: list[RawSignal] = []
    with pytest.raises(KeyboardInterrupt):
        watcher.run_forever(received.append)

    assert len(received) == 2  # only the first pass had new signals
    assert sleep_calls == [config.poll_interval_seconds, config.poll_interval_seconds]
