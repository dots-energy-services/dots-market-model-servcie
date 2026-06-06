from dataclasses import dataclass
from typing import List, Optional

@dataclass
class ClearMarketOutput:
    mcp_vector: Optional[List] = None

