"""
Routing Engine — venue selection with explainable decisions.

Given a matched MarketPair and a hypothetical order, this engine:
1. Scores each venue on price, liquidity, and spread
2. Produces a weighted total score
3. Selects the best venue
4. Generates a human-readable explanation of WHY

The explanation is the key deliverable here — the spec says:
"We are less concerned with optimizing execution quality than with
understanding the reasoning and structure behind the decision."
"""

import logging
from datetime import datetime, timezone
from typing import Optional

from src.models.market import MarketPair, CanonicalMarket
from src.models.routing import (
    HypotheticalOrder,
    OrderSide,
    RoutingDecision,
    RoutingReason,
    VenueScore,
)

logger = logging.getLogger(__name__)


class RoutingEngine:
    """
    Evaluates matched market pairs and routes hypothetical orders.

    Scoring weights are configurable — in a real system these would be
    tuned based on historical execution quality data.
    """

    def __init__(
        self,
        price_weight: float = 0.5,
        liquidity_weight: float = 0.3,
        spread_weight: float = 0.2,
    ):
        self.price_weight = price_weight
        self.liquidity_weight = liquidity_weight
        self.spread_weight = spread_weight

    def route(self, order: HypotheticalOrder) -> RoutingDecision:
        """
        Route a hypothetical order to the best venue.

        Returns a RoutingDecision with full scoring breakdown and explanation.
        """
        pair = order.market_pair
        venues = [
            (pair.market_a.venue.value, pair.market_a),
            (pair.market_b.venue.value, pair.market_b),
        ]

        venue_scores = []
        for venue_name, market in venues:
            score = self._score_venue(market, order.side)
            score.venue = venue_name
            score.market = market
            venue_scores.append(score)

        # Select best venue (highest total score)
        best = max(venue_scores, key=lambda vs: vs.total_score)

        # Determine primary reason
        primary_reason = self._determine_reason(venue_scores, best)

        # Generate explanation
        explanation = self._explain(order, venue_scores, best, primary_reason)

        return RoutingDecision(
            order=order,
            selected_venue=best.venue,
            selected_market=best.market,
            venue_scores=venue_scores,
            primary_reason=primary_reason,
            explanation=explanation,
            decided_at=datetime.now(timezone.utc),
        )

    def _score_venue(self, market: CanonicalMarket, side: OrderSide) -> VenueScore:
        """
        Score a single venue for a given order side.

        Price score: For BUY_YES, lower yes_price = better (cheaper to buy).
                     For BUY_NO, lower no_price = better.
                     Inverted to 0-1 where 1 = best.

        Liquidity score: Higher volume = better. Log-scaled because
                         volume differences can be orders of magnitude.

        Spread score: Lower spread = better (tighter market).
        """
        score = VenueScore(venue="", market=market)

        # Price score (0-1, higher is better for buyer)
        if side == OrderSide.BUY_YES and market.yes_price is not None:
            # Lower price = better for buyer → invert
            score.price_score = 1.0 - market.yes_price
            score.reasoning = f"YES price: ${market.yes_price:.2f}"
        elif side == OrderSide.BUY_NO and market.no_price is not None:
            score.price_score = 1.0 - market.no_price
            score.reasoning = f"NO price: ${market.no_price:.2f}"
        else:
            score.price_score = 0.0
            score.reasoning = "No price available"

        # Liquidity score (0-1, log-scaled)
        if market.volume_usd and market.volume_usd > 0:
            import math
            # Log scale: $1K = 0.3, $100K = 0.5, $10M = 0.7, $1B = 0.9
            score.liquidity_score = min(1.0, math.log10(market.volume_usd + 1) / 10.0)
        else:
            score.liquidity_score = 0.0

        # Spread score (0-1, lower spread = higher score)
        if market.spread is not None:
            # Spread of 0 = perfect = score 1.0
            # Spread of 0.10 (10%) = score 0.0
            score.spread_score = max(0.0, 1.0 - abs(market.spread) * 10)
        else:
            score.spread_score = 0.5  # Unknown — neutral

        # Weighted total
        score.total_score = (
            score.price_score * self.price_weight
            + score.liquidity_score * self.liquidity_weight
            + score.spread_score * self.spread_weight
        )

        return score

    def _determine_reason(self, scores: list[VenueScore], best: VenueScore) -> RoutingReason:
        """Determine the primary reason for venue selection."""
        if len(scores) < 2:
            return RoutingReason.ONLY_VENUE

        other = [s for s in scores if s.venue != best.venue][0]

        # Check which factor dominates
        price_diff = best.price_score - other.price_score
        liquidity_diff = best.liquidity_score - other.liquidity_score
        spread_diff = best.spread_score - other.spread_score

        diffs = {
            RoutingReason.BEST_PRICE: abs(price_diff) * self.price_weight,
            RoutingReason.BEST_LIQUIDITY: abs(liquidity_diff) * self.liquidity_weight,
            RoutingReason.LOWEST_SPREAD: abs(spread_diff) * self.spread_weight,
        }

        dominant = max(diffs, key=diffs.get)

        # If no single factor dominates clearly, it's composite
        max_contrib = max(diffs.values())
        total_contrib = sum(diffs.values())
        if total_contrib > 0 and max_contrib / total_contrib < 0.5:
            return RoutingReason.COMPOSITE

        return dominant

    def _explain(
        self,
        order: HypotheticalOrder,
        scores: list[VenueScore],
        best: VenueScore,
        reason: RoutingReason,
    ) -> str:
        """Generate a human-readable routing explanation."""
        parts = [f"Routing decision for: {order.description}\n"]

        for vs in scores:
            parts.append(
                f"  {vs.venue}: price={vs.price_score:.3f} "
                f"liquidity={vs.liquidity_score:.3f} "
                f"spread={vs.spread_score:.3f} "
                f"→ total={vs.total_score:.3f}"
            )

        parts.append(f"\nSelected: {best.venue}")

        reason_explanations = {
            RoutingReason.BEST_PRICE: "offers the best price for this order side",
            RoutingReason.BEST_LIQUIDITY: "has significantly higher trading volume",
            RoutingReason.LOWEST_SPREAD: "has the tightest spread (most efficient market)",
            RoutingReason.ONLY_VENUE: "is the only venue with this market",
            RoutingReason.COMPOSITE: "scores best across a combination of price, liquidity, and spread",
        }
        parts.append(f"Reason: {best.venue} {reason_explanations.get(reason, 'has the highest composite score')}")

        # Add price context
        pair = order.market_pair
        if pair.price_divergence is not None:
            parts.append(
                f"\nPrice divergence between venues: {pair.price_divergence:.1%} "
                f"({'potential arbitrage opportunity' if pair.price_divergence > 0.05 else 'markets are aligned'})"
            )

        return "\n".join(parts)

    def simulate_orders(self, pairs: list[MarketPair]) -> list[RoutingDecision]:
        """
        Generate and route hypothetical orders for all matched pairs.
        Creates both a BUY_YES and BUY_NO order for each pair.
        """
        decisions = []

        for pair in pairs:
            for side in [OrderSide.BUY_YES, OrderSide.BUY_NO]:
                order = HypotheticalOrder(
                    market_pair=pair,
                    side=side,
                    amount_usd=100.0,  # Standard hypothetical order size
                )
                decision = self.route(order)
                decisions.append(decision)

        return decisions
