"""WSGI entry point for production deployment (gunicorn / uWSGI).

Usage:
    gunicorn -c gunicorn.conf.py wsgi:application
"""

from __future__ import annotations

import yaml
from pathlib import Path

from src.web_server import create_app

_config_path = Path(__file__).parent / "config.yaml"

def _load_config() -> dict:
    if _config_path.exists():
        with _config_path.open() as fh:
            return yaml.safe_load(fh) or {}
    return {}

application = create_app(_load_config())

if __name__ == "__main__":
    application.run(host="0.0.0.0", port=5050, debug=False)
