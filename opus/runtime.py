"""Which Opus is running: the Windows PC assistant, or the phone build."""
from __future__ import annotations

import os

PC = "pc"
PHONE = "phone"

_runtime = (os.environ.get("OPUS_RUNTIME") or PC).strip().lower()
if _runtime not in {PC, PHONE}:
    _runtime = PC


def set_runtime(name: str) -> None:
    global _runtime
    value = (name or PC).strip().lower()
    _runtime = PHONE if value == PHONE else PC
    os.environ["OPUS_RUNTIME"] = _runtime


def current() -> str:
    return _runtime


def is_phone() -> bool:
    return current() == PHONE


def is_pc() -> bool:
    return current() == PC
