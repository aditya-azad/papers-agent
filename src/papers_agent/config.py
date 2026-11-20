import os
import tomllib
from pathlib import Path

_SECTION = "arxiv"


class ConfigError(Exception):
    pass


def _config_path() -> Path:
    config_home = os.environ.get("XDG_CONFIG_HOME") or "~/.config"
    return Path(config_home).expanduser() / "papers-agent" / "config.toml"


def backup_db() -> Path:
    path = _config_path()
    try:
        with path.open("rb") as config_file:
            data = tomllib.load(config_file)
    except FileNotFoundError:
        raise ConfigError(
            f"missing config file {path}; create it with an"
            f" [{_SECTION}] section containing backup_db"
        )
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid config file {path}: {exc}")
    section = data.get(_SECTION)
    if not isinstance(section, dict):
        raise ConfigError(f"config file {path} must have an [{_SECTION}] section")
    value = section.get("backup_db")
    if not isinstance(value, str) or not value:
        raise ConfigError(f"config file {path} must set {_SECTION}.backup_db to a path")
    return Path(value).expanduser()
