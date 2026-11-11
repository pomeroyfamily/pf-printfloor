"""Driver registry: maps driver keys ("bambu.x1c") to classes and builds the
Maker -> Model choices for the add-printer form."""
from __future__ import annotations

from typing import Optional, TypeVar

from .base import PrinterBase

_DRIVERS: dict[str, type[PrinterBase]] = {}
T = TypeVar("T", bound=type[PrinterBase])


def register(cls: T) -> T:
    """Class decorator for concrete (child) printer classes."""
    if not cls.driver_key:
        raise ValueError(f"{cls.__name__} has no driver_key")
    if cls.driver_key in _DRIVERS and _DRIVERS[cls.driver_key] is not cls:
        raise ValueError(f"duplicate driver_key {cls.driver_key}")
    cls.abstract = False
    _DRIVERS[cls.driver_key] = cls
    return cls


def get_driver(key: str) -> type[PrinterBase]:
    try:
        return _DRIVERS[key]
    except KeyError:
        raise KeyError(f"unknown printer driver '{key}'") from None


def all_drivers() -> dict[str, type[PrinterBase]]:
    return dict(_DRIVERS)


def families() -> list[type[PrinterBase]]:
    """One class per protocol family (the direct parents used for discovery)."""
    seen: dict[str, type[PrinterBase]] = {}
    for cls in _DRIVERS.values():
        for base in cls.__mro__:
            if base is not PrinterBase and issubclass(base, PrinterBase) and base.family \
                    and base.__dict__.get("abstract", False):
                seen.setdefault(base.family, base)
                break
    return list(seen.values())


def driver_for_model_code(family: str, code: Optional[str]) -> Optional[type[PrinterBase]]:
    """Bambu model codes (SSDP DevModel / cloud dev_model_name, e.g. "C11") -> child class."""
    for cls in _DRIVERS.values():
        if cls.family == family and code and code in getattr(cls, "ssdp_model_codes", ()):
            return cls
    return None


def catalog() -> list[dict]:
    """Maker -> models -> fields, for the add-printer UI."""
    makers: dict[str, list[dict]] = {}
    for key, cls in sorted(_DRIVERS.items()):
        makers.setdefault(cls.maker, []).append({
            "driver": key,
            "model": cls.model_name,
            "family": cls.family,
            "available": cls.available,
            "fields": [f.model_dump() for f in cls.fields],
        })
    return [{"maker": m, "models": v} for m, v in sorted(makers.items())]
