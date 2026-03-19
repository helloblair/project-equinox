"""
Base Venue Adapter — defines the contract all venue integrations must follow.

This is a key architectural decision: by enforcing a common interface,
the matching and routing layers never need to know which venue they're
working with. New venues can be added by implementing this interface.
"""

from abc import ABC, abstractmethod
from src.models.market import CanonicalMarket


class BaseVenueAdapter(ABC):
    """Abstract base for all venue integrations."""

    @property
    @abstractmethod
    def venue_name(self) -> str:
        """Human-readable venue name."""
        ...

    @abstractmethod
    async def fetch_markets(self, limit: int = 100) -> list[CanonicalMarket]:
        """Fetch and normalize markets from this venue."""
        ...

    @abstractmethod
    def normalize(self, raw_market: dict) -> CanonicalMarket:
        """Convert a single raw venue market to canonical form."""
        ...
