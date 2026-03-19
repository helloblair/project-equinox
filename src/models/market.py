"""
Canonical Market Model — venue-agnostic internal representation.

Every venue's market data gets normalized into this schema before
any matching or routing logic touches it.

Design decisions:
- venue + venue_market_id = unique key across all venues
- Prices normalized to [0.0, 1.0] (implied probability)
- volume_usd always in USD regardless of venue currency
- raw_data preserves original venue payload for audit
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional
from enum import Enum


class MarketStatus(Enum):
    OPEN = "open"
    CLOSED = "closed"
    SETTLED = "settled"
    UNKNOWN = "unknown"


class VenueType(Enum):
    POLYMARKET = "polymarket"
    KALSHI = "kalshi"


@dataclass
class CanonicalMarket:
    """Venue-agnostic market representation."""

    venue: VenueType
    venue_market_id: str
    venue_event_id: str

    title: str
    description: str
    category: str
    tags: list[str] = field(default_factory=list)

    yes_price: Optional[float] = None
    no_price: Optional[float] = None

    volume_usd: Optional[float] = None
    liquidity_usd: Optional[float] = None

    expiration_date: Optional[datetime] = None
    status: MarketStatus = MarketStatus.UNKNOWN

    raw_data: dict = field(default_factory=dict)
    fetched_at: Optional[datetime] = None
    warnings: list[str] = field(default_factory=list)  # Data quality issues logged during normalization

    @property
    def implied_probability(self) -> Optional[float]:
        return self.yes_price

    @property
    def spread(self) -> Optional[float]:
        if self.yes_price is not None and self.no_price is not None:
            return (self.yes_price + self.no_price) - 1.0
        return None

    def to_dict(self) -> dict:
        return {
            "venue": self.venue.value,
            "venue_market_id": self.venue_market_id,
            "venue_event_id": self.venue_event_id,
            "title": self.title,
            "description": self.description,
            "category": self.category,
            "tags": self.tags,
            "yes_price": self.yes_price,
            "no_price": self.no_price,
            "implied_probability": self.implied_probability,
            "spread": self.spread,
            "volume_usd": self.volume_usd,
            "liquidity_usd": self.liquidity_usd,
            "expiration_date": self.expiration_date.isoformat() if self.expiration_date else None,
            "status": self.status.value,
            "fetched_at": self.fetched_at.isoformat() if self.fetched_at else None,
            "warnings": self.warnings,
        }


@dataclass
class MarketPair:
    """A matched pair of markets from different venues representing the same event."""

    market_a: CanonicalMarket
    market_b: CanonicalMarket
    confidence: float
    match_method: str
    match_details: dict = field(default_factory=dict)

    @property
    def price_divergence(self) -> Optional[float]:
        if self.market_a.yes_price is not None and self.market_b.yes_price is not None:
            return abs(self.market_a.yes_price - self.market_b.yes_price)
        return None

    @property
    def venues(self) -> tuple[str, str]:
        return (self.market_a.venue.value, self.market_b.venue.value)

    def to_dict(self) -> dict:
        return {
            "market_a": self.market_a.to_dict(),
            "market_b": self.market_b.to_dict(),
            "confidence": self.confidence,
            "match_method": self.match_method,
            "match_details": self.match_details,
            "price_divergence": self.price_divergence,
        }
