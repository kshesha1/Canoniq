from pathlib import Path

import pytest

from canoniq.config import Config, ConfigError, load_config

EXAMPLE_CONFIG = Path(__file__).parent.parent / "examples" / "canoniq.yaml.example"


def test_example_config_loads() -> None:
    config = load_config(str(EXAMPLE_CONFIG))
    assert isinstance(config, Config)
    assert config.project_name == "my_semantic_model"
    assert config.warehouse_type == "duckdb"
    assert config.output_formats == ["metricflow", "osi"]
    assert config.output_dir == "./canoniq_output/"
    assert config.llm_model == "claude-sonnet-4-6"
    assert config.llm_max_retries == 3
    assert config.continuous_enabled is False
    assert config.poll_interval_seconds == 300


def test_ontorank_weights_default_and_sum() -> None:
    config = load_config(str(EXAMPLE_CONFIG))
    w = config.ontorank_weights
    total = (
        w.source_authority
        + w.usage_frequency
        + w.cross_source_agreement
        + w.recency
        + w.certification_status
    )
    assert total == pytest.approx(1.0)


def test_ontorank_thresholds_ordering() -> None:
    config = load_config(str(EXAMPLE_CONFIG))
    t = config.ontorank_thresholds
    assert t.drop <= t.review <= t.auto_merge


def test_missing_file_raises() -> None:
    with pytest.raises(ConfigError):
        load_config("does_not_exist.yaml")


def test_missing_required_field_raises(tmp_path: Path) -> None:
    bad_config = tmp_path / "canoniq.yaml"
    bad_config.write_text("warehouse:\n  type: duckdb\n")
    with pytest.raises(ConfigError, match="project_name"):
        load_config(str(bad_config))


def test_invalid_warehouse_type_raises(tmp_path: Path) -> None:
    bad_config = tmp_path / "canoniq.yaml"
    bad_config.write_text(
        "project_name: test\n"
        "warehouse:\n  type: mysql\n"
        "output:\n  formats: [osi]\n  dir: ./out\n"
    )
    with pytest.raises(ConfigError, match="warehouse.type"):
        load_config(str(bad_config))


def test_invalid_weights_sum_raises(tmp_path: Path) -> None:
    bad_config = tmp_path / "canoniq.yaml"
    bad_config.write_text(
        "project_name: test\n"
        "warehouse:\n  type: duckdb\n"
        "output:\n  formats: [osi]\n  dir: ./out\n"
        "ontorank:\n  weights:\n    source_authority: 0.5\n"
    )
    with pytest.raises(ConfigError, match="sum to 1.0"):
        load_config(str(bad_config))
