"""Kinde SDK instrumentation for REPLAY mode.

This instrumentation patches StorageManager.get() to handle device ID mismatch
during replay. This enables successful authentication for Flask or FastAPI.

Problem: During replay, Kinde's StorageManager generates a new UUID on server
startup, but the replayed session contains data keyed with the old device ID
(e.g., `device:OLD-UUID:user_id`). The lookup fails because StorageManager
looks for `device:NEW-UUID:user_id`.

Solution: Patch StorageManager.get() to:
1. Try normal lookup with current device_id
2. If that fails, scan session keys for pattern `device:*:{key}`
3. Extract device ID from found key and cache it for future lookups
4. Return the found value

This approach is framework-agnostic - it works with Flask or FastAPI. Kinde does not support Django.

Only active in REPLAY mode.
"""

from __future__ import annotations

import logging
import re
from types import ModuleType
from typing import TYPE_CHECKING, Any

from ...core.types import TuskDriftMode
from ..base import InstrumentationBase

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

# Pattern to extract device ID from session keys: device:{uuid}:{key}
DEVICE_KEY_PATTERN = re.compile(r"^device:([^:]+):(.+)$")


def _get_session_from_storage(storage: Any) -> Any | None:
    """Get the underlying session from a storage adapter.

    Works with FrameworkAwareStorage which supports both Flask and FastAPI.

    FrameworkAwareStorage is a Kinde concept: kinde-python-sdk/kinde_sdk/core/storage/framework_aware_storage.py

    Args:
        storage: The storage adapter instance

    Returns:
        The session object if available, None otherwise.
    """
    if storage is None:
        logger.debug("[KindeInstrumentation] Storage is None")
        return None

    # FrameworkAwareStorage has _get_session() method
    if hasattr(storage, "_get_session"):
        try:
            session = storage._get_session()
            logger.debug(f"[KindeInstrumentation] Got session from storage._get_session(): {session is not None}")
            return session
        except Exception as e:
            logger.debug(f"[KindeInstrumentation] Error calling _get_session(): {e}")

    return None


def _scan_session_for_key(session: Any, target_key: str) -> tuple[str | None, Any | None]:
    """Scan session keys to find a device-prefixed key matching the target.

    Args:
        session: The session object (Flask session, FastAPI session, etc.)
        target_key: The key we're looking for (without device prefix)

    Returns:
        Tuple of (device_id, value) if found, (None, None) otherwise.
    """
    if session is None:
        return None, None

    try:
        # Handle both dict-like sessions and sessions with keys() method
        keys = list(session.keys()) if hasattr(session, "keys") else []
        logger.debug(f"[KindeInstrumentation] Scanning {len(keys)} session keys for '{target_key}'")

        for session_key in keys:
            match = DEVICE_KEY_PATTERN.match(session_key)
            if match:
                device_id = match.group(1)
                key_suffix = match.group(2)
                if key_suffix == target_key:
                    value = session.get(session_key)
                    logger.debug(f"[KindeInstrumentation] Found key '{target_key}' with device ID: {device_id}")
                    return device_id, value
    except Exception as e:
        logger.debug(f"[KindeInstrumentation] Error scanning session: {e}")

    logger.debug(f"[KindeInstrumentation] Key '{target_key}' not found in session")
    return None, None


class KindeInstrumentation(InstrumentationBase):
    """Instrumentation to patch Kinde SDK for REPLAY mode compatibility.

    Patches StorageManager.get() to handle device ID mismatch by scanning
    session keys and extracting the correct device ID from recorded data.
    Works with Flask, FastAPI, and other frameworks using FrameworkAwareStorage.
    """

    def __init__(self, mode: TuskDriftMode = TuskDriftMode.DISABLED, enabled: bool = True) -> None:
        """Initialize Kinde instrumentation.

        Args:
            mode: The SDK mode (RECORD, REPLAY, DISABLED)
            enabled: Whether instrumentation is enabled
        """
        self.mode = mode

        # Only enable in REPLAY mode
        should_enable = enabled and mode == TuskDriftMode.REPLAY

        super().__init__(
            name="KindeInstrumentation",
            module_name="kinde_sdk",
            supported_versions="*",
            enabled=should_enable,
        )

        if should_enable:
            logger.debug("[KindeInstrumentation] Initialized in REPLAY mode")

    def patch(self, module: ModuleType) -> None:
        """Patch the Kinde SDK module.

        Args:
            module: The kinde_sdk module to patch
        """
        logger.debug(f"[KindeInstrumentation] patch() called with module: {module.__name__}")

        if self.mode != TuskDriftMode.REPLAY:
            logger.debug("[KindeInstrumentation] Not in REPLAY mode, skipping patch")
            return

        self._patch_storage_manager_get()

    def _patch_storage_manager_get(self) -> None:
        """Patch StorageManager.get() to handle device ID mismatch during replay."""
        try:
            from kinde_sdk.core.storage.storage_manager import StorageManager

            logger.debug("[KindeInstrumentation] Successfully imported StorageManager")
        except ImportError as e:
            logger.warning(f"[KindeInstrumentation] Could not import StorageManager from kinde_sdk: {e}")
            return

        original_get = StorageManager.get

        def patched_get(self: StorageManager, key: str) -> dict | None:
            """Patched get() that handles device ID mismatch.

            First tries normal lookup. If that fails for a device-specific key,
            scans session for keys with different device IDs and extracts the
            correct device ID for future lookups.

            Args:
                self: The StorageManager instance
                key: The key to retrieve

            Returns:
                The stored data or None if not found.
            """
            logger.debug(f"[KindeInstrumentation] patched_get() called for key: {key}")

            # Try normal lookup first
            result = original_get(self, key)
            if result is not None:
                logger.debug(f"[KindeInstrumentation] Normal lookup succeeded for key: {key}")
                return result

            logger.debug(f"[KindeInstrumentation] Normal lookup failed for key: {key}")

            # Skip special keys that don't use device namespacing
            if key == "_device_id" or key.startswith("global:") or key.startswith("user:"):
                return None

            # Normal lookup failed - try to find key with different device ID
            session = _get_session_from_storage(self._storage)
            if session is None:
                logger.debug("[KindeInstrumentation] Could not get session from storage")
                return None

            # Scan session for this key with any device ID
            found_device_id, found_value = _scan_session_for_key(session, key)

            if found_device_id and found_value is not None:
                # Cache the device ID for future lookups
                with self._lock:
                    if self._device_id != found_device_id:
                        logger.debug(
                            f"[KindeInstrumentation] Updating device ID: {self._device_id} -> {found_device_id}"
                        )
                        self._device_id = found_device_id
                return found_value

            return None

        StorageManager.get = patched_get
        logger.debug("[KindeInstrumentation] Patched StorageManager.get()")
