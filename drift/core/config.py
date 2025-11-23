from dataclasses import dataclass
from typing import Any, Optional


@dataclass
class TuskConfig:
    api_key: Optional[str] = None
    env: Optional[str] = None
    sampling_rate: float = 1.0
    transforms: Optional[dict[str, Any]] = None
