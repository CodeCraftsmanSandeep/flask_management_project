from pathlib import Path

import pytest

from app.config import DEFAULT_TIMEOUT_SECONDS, LOCAL_HOSTS, PROJECT_ROOT, Config


def test_defaults() -> None:
    config = Config.from_env({})
    assert config.keys_file == PROJECT_ROOT / "keys.txt"
    assert config.request_timeout == DEFAULT_TIMEOUT_SECONDS
    assert config.allowed_hosts == LOCAL_HOSTS


def test_environment_overrides(tmp_path: Path) -> None:
    config = Config.from_env(
        {
            "GROWW_KEYS_FILE": str(tmp_path / "other.txt"),
            "GROWW_REQUEST_TIMEOUT": "3",
            "ALLOWED_HOSTS": " Trading.local , ,192.168.1.5 ",
        }
    )
    assert config.keys_file == (tmp_path / "other.txt").resolve()
    assert config.request_timeout == 3
    assert config.allowed_hosts == LOCAL_HOSTS | {"trading.local", "192.168.1.5"}


@pytest.mark.parametrize("value", ["soon", "0", "-5", "1.5"])
def test_invalid_timeout_is_rejected(value: str) -> None:
    with pytest.raises(ValueError, match="GROWW_REQUEST_TIMEOUT"):
        Config.from_env({"GROWW_REQUEST_TIMEOUT": value})


def test_contact_url_defaults_to_the_profile_link() -> None:
    assert Config.from_env({}).contact_url.startswith("https://www.upwork.com/freelancers/")


def test_contact_url_can_be_overridden() -> None:
    assert (
        Config.from_env({"CONTACT_URL": " https://example.com/me "}).contact_url
        == "https://example.com/me"
    )


@pytest.mark.parametrize(
    "value", ["javascript:alert(1)", "http://example.com", "example.com", "data:text/html,x"]
)
def test_contact_url_must_be_https(value: str) -> None:
    with pytest.raises(ValueError, match="https"):
        Config.from_env({"CONTACT_URL": value})
