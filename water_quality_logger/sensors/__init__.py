"""Sensor driver package.

Every module in this directory is imported at startup so that any driver
using @register() becomes available automatically. Dropping a new .py file
in here is all that's needed to add a sensor type.
"""

import importlib
import pkgutil

from .base import Channel, Sensor, create, known_types, register  # noqa: F401

_SKIP = {"base", "modbus", "ads1115"}

for _module in pkgutil.iter_modules(__path__):
    if _module.name.startswith("_") or _module.name in _SKIP:
        continue
    try:
        importlib.import_module(f"{__name__}.{_module.name}")
    except Exception as exc:   # a missing optional dep shouldn't kill everything
        print(f"[sensors] could not load driver '{_module.name}': {exc}")

__all__ = ["Sensor", "Channel", "register", "create", "known_types"]
