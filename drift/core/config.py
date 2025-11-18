from dataclasses import dataclass
from typing import Optional


@dataclass
class TuskConfig:
    api_key: Optional[str] = None
    env: Optional[str] = None
    sampling_rate: float = 1.0
