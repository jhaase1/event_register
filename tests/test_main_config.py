import json

import main


def test_load_app_config_merges_with_defaults(tmp_path):
    config_file = tmp_path / "app_config.json"
    config_file.write_text(json.dumps({"hold_buffer_minutes": 15}), encoding="utf-8")

    config = main.load_app_config(str(config_file))

    assert config["hold_buffer_minutes"] == 15
    assert config["login_buffer_minutes"] == 1
    assert config["min_delay_seconds"] == 4
    assert config["max_delay_seconds"] == 6
    assert config["cleanup_days"] == 8


def test_load_app_config_uses_defaults_when_missing(tmp_path):
    config_file = tmp_path / "missing.json"

    config = main.load_app_config(str(config_file))

    assert config == main.DEFAULT_APP_CONFIG