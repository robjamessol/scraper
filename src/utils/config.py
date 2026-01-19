"""Configuration loading utilities."""

import os
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv


def load_config(config_path: str | Path | None = None) -> dict[str, Any]:
    """
    Load the newsletter configuration from YAML file.

    Args:
        config_path: Path to config file. Defaults to config/newsletters.yaml

    Returns:
        Configuration dictionary
    """
    load_dotenv()

    if config_path is None:
        # Find config relative to project root
        project_root = Path(__file__).parent.parent.parent
        config_path = project_root / "config" / "newsletters.yaml"

    config_path = Path(config_path)

    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")

    with open(config_path, "r") as f:
        config = yaml.safe_load(f)

    return config


def get_newsletter_config(newsletter_id: str, config: dict[str, Any] | None = None) -> dict[str, Any]:
    """
    Get configuration for a specific newsletter.

    Args:
        newsletter_id: Newsletter identifier (e.g., 'healthcare_brew')
        config: Full config dict, or None to load from file

    Returns:
        Newsletter-specific configuration

    Raises:
        KeyError: If newsletter not found in config
    """
    if config is None:
        config = load_config()

    newsletters = config.get("newsletters", {})

    if newsletter_id not in newsletters:
        available = list(newsletters.keys())
        raise KeyError(
            f"Newsletter '{newsletter_id}' not found. "
            f"Available: {available}"
        )

    return newsletters[newsletter_id]


def get_env(key: str, default: str | None = None, required: bool = False) -> str | None:
    """
    Get environment variable with optional default and required flag.

    Args:
        key: Environment variable name
        default: Default value if not set
        required: If True, raise error when not set

    Returns:
        Environment variable value

    Raises:
        ValueError: If required and not set
    """
    value = os.getenv(key, default)

    if required and value is None:
        raise ValueError(f"Required environment variable not set: {key}")

    return value
