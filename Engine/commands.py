"""CLI command builders shared by source and the self-contained Mac engine."""
from pathlib import Path
import sys


def engine_command(command: str) -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable, command]
    return [sys.executable, str(Path(__file__).with_name("engine.py")), command]
