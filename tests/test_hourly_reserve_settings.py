"""Политика резерва включается явно, значения старых настроек не меняются."""
import copy

import pytest

from max_postiz_agent.config import AgentError
from max_postiz_agent.server_state import default_settings, validate_settings


def test_legacy_settings_are_read_as_disabled_without_mutating_saved_document():
    value = default_settings()
    value.pop("reserve")
    original = copy.deepcopy(value)
    result = validate_settings(value)
    assert value == original and "reserve" not in value
    assert result["reserve"] == {"enabled": False, "target": 6, "low": 3, "maximum": 12, "batch": 3}


@pytest.mark.parametrize(("key", "value"), [
    ("enabled", 1), ("target", True), ("target", 3), ("low", 0),
    ("low", 6), ("maximum", 5), ("maximum", 25), ("batch", 0), ("batch", 4),
])
def test_invalid_reserve_settings_cannot_change_policy(key, value):
    settings = default_settings()
    settings["reserve"][key] = value
    with pytest.raises(AgentError, match="EDITORIAL_SETTINGS_INVALID"):
        validate_settings(settings)


def test_valid_configurable_bounds_preserve_filters_and_interval():
    settings = default_settings()
    settings["reserve"] = {"enabled": True, "target": 8, "low": 2, "maximum": 16, "batch": 2}
    settings["nasa"]["authors"] = ["NASA"]
    assert validate_settings(settings) == settings
