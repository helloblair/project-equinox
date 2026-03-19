"""
Equinox Pipeline — orchestrates the full fetch → normalize → match → route flow.

This is the main entry point that ties all components together.
Each stage is independent and testable in isolation.
"""

import asyncio
import json
import logging
from datetime import datetime, timezone

from src.venues.polymarket import PolymarketAdapter
from src.venues.kalshi import KalshiAdapter
from src.matching.engine import EquivalenceEngine
from src.routing.engine import RoutingEngine
from src.models.market import CanonicalMarket, MarketPair
from src.models.routing import RoutingDecision

logger = logging.getLogger(__name__)


class EquinoxPipeline:
    """
    Full pipeline: ingest → normalize → match → route.

    Each run produces:
    - Normalized markets from both venues
    - Matched pairs with confidence scores
    - Routing decisions with explanations
    """

    def __init__(self):
        self.polymarket = PolymarketAdapter()
        self.kalshi = KalshiAdapter()
        self.matcher = EquivalenceEngine(min_confidence=0.55)
        self.router = RoutingEngine()

        # Pipeline state
        self.poly_markets: list[CanonicalMarket] = []
        self.kalshi_markets: list[CanonicalMarket] = []
        self.matches: list[MarketPair] = []
        self.decisions: list[RoutingDecision] = []
        self.run_timestamp: datetime | None = None

    async def run(self, market_limit: int = 100) -> dict:
        """Execute the full pipeline and return results."""
        self.run_timestamp = datetime.now(timezone.utc)
        logger.info(f"Starting Equinox pipeline at {self.run_timestamp.isoformat()}")

        # Stage 1: Fetch and normalize
        logger.info("Stage 1: Fetching markets from both venues...")
        self.poly_markets, self.kalshi_markets = await asyncio.gather(
            self.polymarket.fetch_markets(limit=market_limit),
            self.kalshi.fetch_markets(limit=market_limit),
        )
        logger.info(
            f"  Polymarket: {len(self.poly_markets)} markets, "
            f"Kalshi: {len(self.kalshi_markets)} markets"
        )

        # Stage 2: Equivalence detection
        logger.info("Stage 2: Detecting equivalent markets...")
        self.matches = self.matcher.find_matches(self.poly_markets, self.kalshi_markets)
        logger.info(f"  Found {len(self.matches)} matched pairs")

        # Stage 3: Routing simulation
        logger.info("Stage 3: Simulating routing decisions...")
        self.decisions = self.router.simulate_orders(self.matches)
        logger.info(f"  Generated {len(self.decisions)} routing decisions")

        # Cleanup
        await self.polymarket.close()
        await self.kalshi.close()

        return self.get_results()

    def get_results(self) -> dict:
        """Package pipeline results as a serializable dict."""
        return {
            "run_timestamp": self.run_timestamp.isoformat() if self.run_timestamp else None,
            "summary": {
                "polymarket_markets": len(self.poly_markets),
                "kalshi_markets": len(self.kalshi_markets),
                "matched_pairs": len(self.matches),
                "ambiguous_pairs": len(self.matcher.ambiguous_pairs),
                "routing_decisions": len(self.decisions),
            },
            "matches": [m.to_dict() for m in self.matches[:20]],  # Top 20 by confidence
            "ambiguous_matches": [m.to_dict() for m in self.matcher.ambiguous_pairs[:10]],
            "routing_decisions": [d.to_dict() for d in self.decisions[:20]],
            "category_breakdown": self._category_breakdown(),
        }

    def _category_breakdown(self) -> dict:
        """Summarize markets by category across venues."""
        poly_cats: dict[str, int] = {}
        kalshi_cats: dict[str, int] = {}

        for m in self.poly_markets:
            poly_cats[m.category] = poly_cats.get(m.category, 0) + 1
        for m in self.kalshi_markets:
            kalshi_cats[m.category] = kalshi_cats.get(m.category, 0) + 1

        all_cats = set(poly_cats.keys()) | set(kalshi_cats.keys())
        return {
            cat: {
                "polymarket": poly_cats.get(cat, 0),
                "kalshi": kalshi_cats.get(cat, 0),
            }
            for cat in sorted(all_cats)
        }


async def main():
    """CLI entry point for running the pipeline."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    pipeline = EquinoxPipeline()
    results = await pipeline.run(market_limit=50)

    print("\n" + "=" * 70)
    print("EQUINOX PIPELINE RESULTS")
    print("=" * 70)
    print(f"\nRun: {results['run_timestamp']}")
    print(f"Polymarket markets: {results['summary']['polymarket_markets']}")
    print(f"Kalshi markets: {results['summary']['kalshi_markets']}")
    print(f"Matched pairs: {results['summary']['matched_pairs']}")
    print(f"Routing decisions: {results['summary']['routing_decisions']}")

    print("\n--- Category Breakdown ---")
    for cat, counts in results["category_breakdown"].items():
        print(f"  {cat}: Polymarket={counts['polymarket']}, Kalshi={counts['kalshi']}")

    if results["matches"]:
        print("\n--- Top Matches ---")
        for i, match in enumerate(results["matches"][:5], 1):
            print(f"\n  #{i} (confidence: {match['confidence']:.2%})")
            print(f"    Polymarket: {match['market_a']['title'][:60]}")
            print(f"    Kalshi:     {match['market_b']['title'][:60]}")
            if match["price_divergence"] is not None:
                print(f"    Price divergence: {match['price_divergence']:.1%}")

    if results["routing_decisions"]:
        print("\n--- Sample Routing Decisions ---")
        for i, decision in enumerate(results["routing_decisions"][:4], 1):
            print(f"\n  #{i}: {decision['order_description']}")
            print(f"    → Route to: {decision['selected_venue']}")
            print(f"    Reason: {decision['primary_reason']}")


if __name__ == "__main__":
    asyncio.run(main())
