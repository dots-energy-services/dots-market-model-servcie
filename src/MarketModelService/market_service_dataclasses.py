from dataclasses import dataclass
from typing import List

@dataclass
class SendCurrentDayAheadPriceOutput:
    day_ahead_price : float | None = None

