"""Core abstractions: a Sensor base class, channel metadata, and a registry.

To add a new sensor type you write one module in this package containing a
class decorated with @register("your_type_name"). Nothing else in the project
needs to change -- the package auto-imports every driver, and main.py builds
whatever config.yaml asks for.
"""

from __future__ import annotations

from dataclasses import dataclass

# type name -> Sensor subclass
_REGISTRY: dict[str, type["Sensor"]] = {}


def register(type_name: str):
    """Class decorator that makes a driver constructible from config.yaml."""

    def decorator(cls):
        if type_name in _REGISTRY:
            raise ValueError(f"duplicate sensor type '{type_name}'")
        cls.TYPE_NAME = type_name
        _REGISTRY[type_name] = cls
        return cls

    return decorator


def create(type_name: str, name: str, params: dict) -> "Sensor":
    try:
        cls = _REGISTRY[type_name]
    except KeyError:
        known = ", ".join(sorted(_REGISTRY)) or "<none>"
        raise KeyError(f"unknown sensor type '{type_name}'. Known types: {known}")
    return cls(name=name, **params)


def known_types() -> list[str]:
    return sorted(_REGISTRY)


@dataclass(frozen=True)
class Channel:
    """One value a sensor produces, i.e. one CSV column."""

    key: str            # short name, e.g. "mg_l"
    unit: str           # e.g. "mg/L" -- goes in the units sidecar, not the header
    decimals: int = 2   # formatting precision in the CSV


class Sensor:
    """Base class for every sensor driver.

    Subclasses must define CHANNELS and implement read(). open() and close()
    are optional; the runner calls open() lazily and will retry it after a
    failure, so a sensor that is unplugged mid-run recovers on its own.
    """

    TYPE_NAME: str = "base"
    CHANNELS: tuple[Channel, ...] = ()

    def __init__(self, name: str, **_ignored):
        self.name = name

    # --- lifecycle -----------------------------------------------------
    def open(self) -> None:
        """Acquire hardware resources. Raise on failure."""

    def read(self) -> dict[str, float]:
        """Return {channel.key: value}. Raise on failure."""
        raise NotImplementedError

    def close(self) -> None:
        """Release hardware resources. Must not raise."""

    # --- schema --------------------------------------------------------
    @property
    def columns(self) -> list[str]:
        """CSV column names, namespaced by this sensor's configured name."""
        return [f"{self.name}_{ch.key}" for ch in self.CHANNELS]

    def format_row(self, values: dict[str, float] | None) -> list[str]:
        """Format a reading into CSV cells. None -> all blanks."""
        cells = []
        for ch in self.CHANNELS:
            value = None if values is None else values.get(ch.key)
            cells.append("" if value is None else f"{value:.{ch.decimals}f}")
        return cells

    def __repr__(self) -> str:
        return f"<{type(self).__name__} name={self.name!r}>"
