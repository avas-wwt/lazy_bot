"""Platform adapters, looked up by the names used in PLATFORMS."""

from __future__ import annotations

from ..config import ConfigError, Settings
from .base import Platform, PlatformError
from .clockify import ClockifyPlatform
from .fake import FakePlatform
from .globalsource import GlobalSourcePlatform
from .kronos import KronosPlatform
from .timesheetportal import TimesheetPortalPlatform

ADAPTERS: dict[str, type[Platform]] = {
    cls.name: cls
    for cls in (ClockifyPlatform, TimesheetPortalPlatform, KronosPlatform, GlobalSourcePlatform, FakePlatform)
}

__all__ = ["ADAPTERS", "Platform", "PlatformError", "adapter_class", "make_platform"]


def adapter_class(name: str) -> type[Platform]:
    try:
        return ADAPTERS[name.lower()]
    except KeyError:
        known = ", ".join(n for n in ADAPTERS if n != "fake")
        raise ConfigError(f"unknown platform {name!r} (known: {known})") from None


def make_platform(settings: Settings, name: str, cache: dict) -> Platform:
    cls = adapter_class(name)
    return cls(settings.platform(cls.name), settings, cache)
