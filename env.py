"""
Load settings from .env (next to this file) into os.environ.

Plain KEY=value lines; blank lines and # comments are skipped, and optional
quotes around the value are stripped. Variables already set in the real
environment win over .env, so `GT7_PS5_IP=... python gt7_web.py` still works.
"""

import os
from pathlib import Path

ENV_FILE = Path(__file__).parent / ".env"


def load_env(path: Path = ENV_FILE) -> None:
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.removeprefix("export ").strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key, value)


load_env()
