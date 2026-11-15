"""DataFeed Base Interface."""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Iterator, Union
from engine.models import MinuteBar, TickData


class IDataFeed(ABC):
    @abstractmethod
    def stream_events(self) -> Iterator[Union[MinuteBar, TickData]]:
        """Yields bars or ticks chronologically."""
        pass

