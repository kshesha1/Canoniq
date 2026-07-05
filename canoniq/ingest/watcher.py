"""Event-driven file/poll watcher loop for continuous ingestion.

v0 sources: QueryLogFileConnector (polls the query log file for new SQL
shapes) and DbtManifestConnector (file-watches manifest.json for changes).
Tableau/Looker/Notion/Slack connectors are deferred to a later milestone.
"""

import hashlib
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from canoniq.config import Config
from canoniq.ingest.base import Connector
from canoniq.ingest.dbt_manifest import DbtManifestConnector
from canoniq.ingest.query_log import QueryLogFileConnector

logger = logging.getLogger(__name__)


@dataclass
class RawSignal:
    """A new piece of evidence discovered by the watcher."""

    signal_type: str    # "query_log" | "dbt_manifest"
    payload: Any         # RawQuery for query_log; a dbt metric dict for dbt_manifest
    signal_hash: str     # stable id used for deduplication


def _signal_hash(signal_type: str, key: str) -> str:
    return hashlib.sha256(f"{signal_type}:{key}".encode()).hexdigest()


class SignalWatcher:
    """
    Polls configured sources for new signals and emits them to the pipeline.
    Runs in a background thread when continuous mode is enabled.
    """

    def __init__(self, config: Config, connectors: list[Connector]):
        self.config = config
        self.connectors = connectors
        self._seen_hashes: set[str] = set()   # deduplicate signals

    def run_once(self) -> list[RawSignal]:
        """Single poll pass — usable in batch mode too."""
        new_signals: list[RawSignal] = []

        for connector in self.connectors:
            if isinstance(connector, QueryLogFileConnector):
                new_signals.extend(self._poll_query_log(connector))
            elif isinstance(connector, DbtManifestConnector):
                new_signals.extend(self._poll_dbt_manifest(connector))
            else:
                logger.warning(
                    "SignalWatcher has no poll strategy for %s", type(connector).__name__
                )

        return new_signals

    def _poll_query_log(self, connector: QueryLogFileConnector) -> list[RawSignal]:
        try:
            queries = connector.get_query_log()
        except Exception as e:
            logger.warning("Failed to poll query log %s: %s", connector.path, e)
            return []

        signals = []
        for query in queries:
            signal_hash = _signal_hash("query_log", query.sql)
            if signal_hash in self._seen_hashes:
                continue
            self._seen_hashes.add(signal_hash)
            signals.append(
                RawSignal(signal_type="query_log", payload=query, signal_hash=signal_hash)
            )
        return signals

    def _poll_dbt_manifest(self, connector: DbtManifestConnector) -> list[RawSignal]:
        try:
            metrics = connector.get_dbt_metrics()
        except Exception as e:
            logger.warning("Failed to poll dbt manifest %s: %s", connector.path, e)
            return []

        signals = []
        for metric in metrics:
            key = f"{metric.get('table')}.{metric.get('name')}.{metric.get('expression')}"
            signal_hash = _signal_hash("dbt_manifest", key)
            if signal_hash in self._seen_hashes:
                continue
            self._seen_hashes.add(signal_hash)
            signals.append(
                RawSignal(signal_type="dbt_manifest", payload=metric, signal_hash=signal_hash)
            )
        return signals

    def run_forever(self, callback: Callable[[RawSignal], None]) -> None:
        """Event loop: poll every poll_interval_seconds."""
        while True:
            new_signals = self.run_once()
            for signal in new_signals:
                callback(signal)
            time.sleep(self.config.poll_interval_seconds)
