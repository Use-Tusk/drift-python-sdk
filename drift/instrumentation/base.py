from abc import ABC, abstractmethod
from types import ModuleType

from .registry import register_patch


class InstrumentationBase(ABC):
    name: str
    module_name: str
    supported_versions: str
    enabled: bool

    def __init__(
        self,
        name: str,
        module_name: str,
        supported_versions: str = "*",
        enabled: bool = True,
    ):
        self.name = name
        self.module_name = module_name
        self.supported_versions = supported_versions
        self.enabled = enabled

        if self.enabled:
            self._register()

    def _register(self) -> None:
        register_patch(self.module_name, self.patch)

    @abstractmethod
    def patch(self, module: ModuleType) -> None:
        pass

    def get_version(self, module: ModuleType) -> str | None:
        return getattr(module, "__version__", None)

    def is_version_supported(self, _: ModuleType) -> bool:
        return True
