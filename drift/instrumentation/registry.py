import gc
import importlib.abc
import importlib.machinery
import sys
from collections.abc import Sequence
from types import ModuleType
from typing import Callable, override

PatchFn = Callable[[ModuleType], None]

_registry: dict[str, PatchFn] = {}
_installed = False


def register_patch(module_name: str, patch_fn: PatchFn) -> None:
    _registry[module_name] = patch_fn


def install_hooks() -> None:
    _install_meta_path_finder()
    _patch_preimported_modules()


class _DriftLoader(importlib.abc.Loader):
    _loader: importlib.abc.Loader
    _patch_fn: PatchFn

    def __init__(self, loader: importlib.abc.Loader, patch_fn: PatchFn) -> None:
        self._loader = loader
        self._patch_fn = patch_fn

    @override
    def create_module(self, spec: importlib.machinery.ModuleSpec):
        # TODO: is this always callable?
        create = self._loader.create_module
        if callable(create):
            return create(spec)
        return None

    @override
    def exec_module(self, module: ModuleType) -> None:
        self._loader.exec_module(module)
        _apply_patch(module, self._patch_fn)


class _DriftFinder(importlib.abc.MetaPathFinder):
    @override
    def find_spec(
        self,
        fullname: str,
        path: Sequence[str] | None,
        target: ModuleType | None = None,
    ):
        patch_fn = _registry.get(fullname)
        if not patch_fn:
            return None

        spec = importlib.machinery.PathFinder.find_spec(fullname, path)
        if not spec or not spec.loader:
            return None

        spec.loader = _DriftLoader(spec.loader, patch_fn)
        return spec


def _install_meta_path_finder() -> None:
    global _installed
    if _installed:
        return

    sys.meta_path.insert(0, _DriftFinder())
    _installed = True


def _patch_preimported_modules() -> None:
    for name, module in list(sys.modules.items()):
        patch_fn = _registry.get(name)
        if patch_fn and module:
            _apply_patch(module, patch_fn)


def _apply_patch(module: ModuleType, patch_fn: PatchFn) -> None:
    if getattr(module, "__drift_patched__", False):
        return

    patch_fn(module)
    module.__drift_patched__ = True  # pyright: ignore[reportAttributeAccessIssue]


from typing import TypeVar

T = TypeVar("T")


def patch_instances_via_gc(class_type: type, patch_instance_fn: Callable[[T], None]) -> None:
    """Use gc to patch instances created before SDK initialization"""
    for obj in gc.get_objects():  # pyright: ignore[reportAny]
        if isinstance(obj, class_type):
            obj: T = obj
            if not getattr(obj, "__drift_instance_patched__", False):
                patch_instance_fn(obj)
                setattr(obj, "__drift_instance_patched__", True)
