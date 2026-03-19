"""
Routing Decision Model — captures venue selection reasoning.

The Equinox spec says: "produce a decision, and clearly explain why
that venue was selected." This model captures the full reasoning chain.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional
from enum import Enum

from src.models.market import CanonicalMarket, MarketPair


class OrderSide(Enum):
    BUY_YES = "buy_yes"
    BUY_NO = "buy_no"


class RoutingReason(Enum):
    BEST_PRICE = "best_price"
    BEST_LIQUIDITY = "best_liquidity"
    LOWEST_SPREAD = "lowest_spread"
    ONLY_VENUE = "only_venue"
    COMPOSITE = "composite"


@dataclass
class HypotheticalOrder:
    market_pair: MarketPair
    side: OrderSide
    amount_usd: float

    @property
    def description(self) -> str:
        side_label = "YES" if self.side == OrderSide.BUY_YES else "NO"
        return f"Buy {side_label} on '{self.market_pair.market_a.title}' for ${self.amount_usd:.2f}"


@dataclass
class VenueScore:
    venue: str
    market: CanonicalMarket
    price_score: float = 0.0
    liquidity_score: float = 0.0
    spread_score: float = 0.0
    total_score: float = 0.0
    reasoning: str = ""


@dataclass
class RoutingDecision:
    order: HypotheticalOrder
    selected_venue: str
    selected_market: CanonicalMarket
    venue_scores: list[VenueScore] = field(default_factory=list)
    primary_reason: RoutingReason = RoutingReason.COMPOSITE
    explanation: str = ""
    decided_at: Optional[datetime] = None

    def to_dict(self) -> dict:
        return {
            "order_description": self.order.description,
            "selected_venue": self.selected_venue,
            "primary_reason": self.primary_reason.value,
            "explanation": self.explanation,
            "venue_scores": [
                {
                    "venue": vs.venue,
                    "price_score": round(vs.price_score, 4),
                    "liquidity_score": round(vs.liquidity_score, 4),
                    "spread_score": round(vs.spread_score, 4),
                    "total_score": round(vs.total_score, 4),
                    "reasoning": vs.reasoning,
                }
                for vs in self.venue_scores
            ],
            "decided_at": self.decided_at.isoformat() if self.decided_at else None,
        }
