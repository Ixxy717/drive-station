from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Callable, Optional

from ..models import DriveInfo, WipeMethod


class HardwareError(Exception):
    """Base for hardware-layer failures."""


class DriveDisconnected(HardwareError):
    """Drive vanished (yanked, bridge reset, power issue)."""


class IdentityError(HardwareError):
    """Drive identity could not be read or is ambiguous."""


class WipeError(HardwareError):
    """Wipe command failed or was rejected."""


class VerifyError(HardwareError):
    """Post-wipe verification found a problem."""


ProgressCallback = Callable[[float], None]


class HardwareBackend(ABC):
    """Interface between the station controller and physical (or fake) docks.

    Implementations must only ever surface allowlisted slots; anything else
    on the host (OS drive, random USB sticks) must be invisible above this
    layer.
    """

    @abstractmethod
    def start(self,
              on_insert: Callable[[str], None],
              on_remove: Callable[[str], None]) -> None:
        """Begin watching for hot-plug events; callbacks receive slot ids."""

    @abstractmethod
    def read_identity(self, slot_id: str) -> Optional[DriveInfo]:
        """Read drive identity fresh from the hardware. None if unreadable."""

    @abstractmethod
    def read_health(self, slot_id: str) -> dict:
        """Return raw health/SMART data for the drive in the slot."""

    def read_usage(self, slot_id: str) -> dict:
        """Pre-wipe used-space probe. Optional; default empty."""
        return {}

    @abstractmethod
    def supported_wipe_methods(self, slot_id: str) -> list[WipeMethod]:
        """Wipe methods that actually work for this drive through this dock."""

    @abstractmethod
    def wipe(self, slot_id: str, method: WipeMethod,
             progress: ProgressCallback) -> None:
        """Execute the wipe. Raises DriveDisconnected/WipeError on failure."""

    @abstractmethod
    def verify(self, slot_id: str, method: WipeMethod,
               progress: ProgressCallback) -> None:
        """Verify the wipe per the method's policy. Raises VerifyError."""
