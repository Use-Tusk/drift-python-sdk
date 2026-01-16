"""Kinde SDK instrumentation for REPLAY mode.

This instrumentation patches Kinde SDK for replay compatibility using a two-tier approach:

1. PRIMARY: Patch StorageManager.get() to handle device ID mismatch
   - During replay, Kinde's StorageManager generates a new UUID on server startup
   - But the replayed session contains data keyed with the old device ID
   - This patch scans session keys for pattern `device:*:{key}` and extracts the correct device ID

2. FALLBACK: Patch all is_authenticated() methods/functions to return True
   - OAuth.is_authenticated()
   - UserSession.is_authenticated()
   - Tokens.is_authenticated()
   - helpers.is_authenticated()
   - If StorageManager patch doesn't help (e.g., app stores auth state elsewhere)
   - Return True anyway since we're replaying known-good authenticated requests

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


def _patch_is_authenticated_method(cls: type, method_name: str, class_name: str) -> bool:
    """Patch an is_authenticated method on a class to return True as fallback.

    Args:
        cls: The class containing the method
        method_name: Name of the method to patch
        class_name: Display name for logging

    Returns:
        True if patching succeeded, False otherwise.
    """
    original = getattr(cls, method_name)

    def patched(*args: Any, **kwargs: Any) -> bool:
        result = original(*args, **kwargs)
        if result:
            logger.debug(f"[KindeInstrumentation] {class_name}.{method_name}() returned True")
            return True
        logger.debug(
            f"[KindeInstrumentation] {class_name}.{method_name}() returned False, "
            "using REPLAY mode fallback (returning True)"
        )
        return True

    setattr(cls, method_name, patched)
    logger.debug(f"[KindeInstrumentation] Patched {class_name}.{method_name}()")
    return True


def _patch_is_authenticated_function(module: ModuleType, func_name: str, module_name: str) -> bool:
    """Patch a standalone is_authenticated function to return True as fallback.

    Args:
        module: The module containing the function
        func_name: Name of the function to patch
        module_name: Display name for logging

    Returns:
        True if patching succeeded, False otherwise.
    """
    original = getattr(module, func_name)

    def patched(*args: Any, **kwargs: Any) -> bool:
        result = original(*args, **kwargs)
        if result:
            logger.debug(f"[KindeInstrumentation] {module_name}.{func_name}() returned True")
            return True
        logger.debug(
            f"[KindeInstrumentation] {module_name}.{func_name}() returned False, "
            "using REPLAY mode fallback (returning True)"
        )
        return True

    setattr(module, func_name, patched)
    logger.debug(f"[KindeInstrumentation] Patched {module_name}.{func_name}()")
    return True


class KindeInstrumentation(InstrumentationBase):
    """Instrumentation to patch Kinde SDK for REPLAY mode compatibility.

    Uses a two-tier approach:
    1. Patches StorageManager.get() to handle device ID mismatch by scanning
       session keys and extracting the correct device ID from recorded data.
    2. Patches all is_authenticated() methods/functions as a fallback to return
       True if the StorageManager approach doesn't work:
       - OAuth.is_authenticated()
       - UserSession.is_authenticated()
       - Tokens.is_authenticated()
       - helpers.is_authenticated()

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
            supported_versions=">=2.0.1",
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

        # Primary patch: handle device ID mismatch in StorageManager
        self._patch_storage_manager_get()

        # Fallback patches: if StorageManager patch doesn't help, force is_authenticated to return True
        self._patch_all_is_authenticated_methods()

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

    def _patch_all_is_authenticated_methods(self) -> None:
        """Patch all is_authenticated methods/functions in Kinde SDK.

        This patches:
        - OAuth.is_authenticated()
        - UserSession.is_authenticated()
        - Tokens.is_authenticated()
        - helpers.is_authenticated()

        Each patch wraps the original to return True as a fallback when the
        original returns False, since we're replaying known-good authenticated requests.
        """
        # Patch OAuth.is_authenticated
        try:
            from kinde_sdk.auth.oauth import OAuth

            _patch_is_authenticated_method(OAuth, "is_authenticated", "OAuth")
        except ImportError:
            logger.debug("[KindeInstrumentation] Could not import OAuth, skipping patch")

        # Patch UserSession.is_authenticated
        try:
            from kinde_sdk.auth.user_session import UserSession

            _patch_is_authenticated_method(UserSession, "is_authenticated", "UserSession")
        except ImportError:
            logger.debug("[KindeInstrumentation] Could not import UserSession, skipping patch")

        # Patch Tokens.is_authenticated
        try:
            from kinde_sdk.auth.tokens import Tokens

            _patch_is_authenticated_method(Tokens, "is_authenticated", "Tokens")
        except ImportError:
            logger.debug("[KindeInstrumentation] Could not import Tokens, skipping patch")

        # Patch helpers.is_authenticated (standalone function)
        try:
            from kinde_sdk.core import helpers

            _patch_is_authenticated_function(helpers, "is_authenticated", "helpers")
        except ImportError:
            logger.debug("[KindeInstrumentation] Could not import helpers, skipping patch")
