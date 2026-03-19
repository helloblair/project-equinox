"""
Test Suite for Project Equinox

Tests are organized by component:
1. Model tests — canonical market creation and properties
2. Normalization tests — venue-specific data → canonical form
3. Matching tests — equivalence detection logic
4. Routing tests — venue selection and explanation
5. Pipeline integration test — end-to-end flow
"""

import asyncio
import pytest
from datetime import datetime, timezone, timedelta

from src.models.market import CanonicalMarket, MarketPair, MarketStatus, VenueType
from src.models.routing import HypotheticalOrder, OrderSide
from src.venues.polymarket import PolymarketAdapter
from src.venues.kalshi import KalshiAdapter
from src.matching.engine import EquivalenceEngine
from src.matching.engine import EquivalenceEngine
from src.routing.engine import RoutingEngine
from src.pipeline import EquinoxPipeline


# ============================================================
# Fixtures
# ============================================================

def make_market(
    venue=VenueType.POLYMARKET,
    title="Test Market",
    description="Test description",
    category="politics",
    yes_price=0.60,
    no_price=0.40,
    volume=1000000.0,
    expiration_days=30,
    market_id="test-001",
) -> CanonicalMarket:
    return CanonicalMarket(
        venue=venue,
        venue_market_id=market_id,
        venue_event_id="evt-001",
        title=title,
        description=description,
        category=category,
        yes_price=yes_price,
        no_price=no_price,
        volume_usd=volume,
        expiration_date=datetime.now(timezone.utc) + timedelta(days=expiration_days),
        status=MarketStatus.OPEN,
        fetched_at=datetime.now(timezone.utc),
    )


# ============================================================
# 1. Model Tests
# ============================================================

class TestCanonicalMarket:

    def test_implied_probability_equals_yes_price(self):
        m = make_market(yes_price=0.65)
        assert m.implied_probability == 0.65

    def test_spread_calculation_perfect_market(self):
        m = make_market(yes_price=0.60, no_price=0.40)
        assert m.spread == pytest.approx(0.0, abs=1e-10)

    def test_spread_calculation_with_vig(self):
        m = make_market(yes_price=0.55, no_price=0.50)
        assert m.spread == pytest.approx(0.05, abs=1e-10)

    def test_spread_none_when_prices_missing(self):
        m = make_market(yes_price=0.60, no_price=None)
        assert m.spread is None

    def test_to_dict_serializable(self):
        m = make_market()
        d = m.to_dict()
        assert d["venue"] == "polymarket"
        assert d["yes_price"] == 0.60
        assert isinstance(d["fetched_at"], str)


class TestMarketPair:

    def test_price_divergence(self):
        a = make_market(venue=VenueType.POLYMARKET, yes_price=0.60)
        b = make_market(venue=VenueType.KALSHI, yes_price=0.65)
        pair = MarketPair(market_a=a, market_b=b, confidence=0.8, match_method="test")
        assert pair.price_divergence == pytest.approx(0.05)

    def test_venues_tuple(self):
        a = make_market(venue=VenueType.POLYMARKET)
        b = make_market(venue=VenueType.KALSHI)
        pair = MarketPair(market_a=a, market_b=b, confidence=0.8, match_method="test")
        assert pair.venues == ("polymarket", "kalshi")


# ============================================================
# 2. Normalization Tests
# ============================================================

class TestPolymarketNormalization:

    def test_parse_stringified_prices(self):
        adapter = PolymarketAdapter()
        raw = {
            "id": "test-pm",
            "question": "Will it rain?",
            "outcomePrices": '["0.70", "0.30"]',
            "active": True,
            "closed": False,
            "volume": "500000",
        }
        event = {"id": "evt-1", "tags": [{"label": "Weather"}]}
        market = adapter.normalize(raw, event_context=event)

        assert market is not None
        assert market.yes_price == pytest.approx(0.70)
        assert market.no_price == pytest.approx(0.30)
        assert market.venue == VenueType.POLYMARKET

    def test_category_from_event_tags(self):
        adapter = PolymarketAdapter()
        raw = {
            "id": "test-pm-2",
            "question": "Test?",
            "outcomePrices": '["0.50", "0.50"]',
            "active": True,
            "closed": False,
        }
        event = {"id": "evt-2", "tags": [{"label": "Crypto"}, {"label": "Bitcoin"}]}
        market = adapter.normalize(raw, event_context=event)

        assert market.category == "crypto"
        assert "Crypto" in market.tags


class TestKalshiNormalization:

    def test_dollar_string_prices(self):
        adapter = KalshiAdapter()
        raw = {
            "ticker": "TEST-001",
            "event_ticker": "TEST",
            "series_ticker": "KXTEST",
            "title": "Test market",
            "status": "open",
            "yes_ask_dollars": "0.6500",
            "no_ask_dollars": "0.3800",
        }
        market = adapter.normalize(raw)

        assert market is not None
        assert market.yes_price == pytest.approx(0.65)
        assert market.no_price == pytest.approx(0.38)

    def test_category_inference_from_series(self):
        adapter = KalshiAdapter()
        raw = {
            "ticker": "KXFED-001",
            "event_ticker": "KXFED",
            "series_ticker": "KXFED",
            "title": "Fed rate decision",
            "status": "open",
            "yes_ask_dollars": "0.50",
        }
        market = adapter.normalize(raw)
        assert market.category == "economics"

    def test_cent_prices_normalized(self):
        """If price > 1.0, assume cents and divide by 100."""
        adapter = KalshiAdapter()
        raw = {
            "ticker": "TEST-002",
            "event_ticker": "TEST",
            "series_ticker": "KXTEST",
            "title": "Test",
            "status": "open",
            "yes_ask_dollars": "65",  # 65 cents
        }
        market = adapter.normalize(raw)
        assert market.yes_price == pytest.approx(0.65)


# ============================================================
# 3. Matching Tests
# ============================================================

class TestEquivalenceEngine:

    def setup_method(self):
        self.engine = EquivalenceEngine(min_confidence=0.3)

    def test_identical_titles_high_confidence(self):
        a = make_market(
            venue=VenueType.POLYMARKET,
            title="Will Bitcoin reach $100k?",
            category="crypto",
        )
        b = make_market(
            venue=VenueType.KALSHI,
            title="Will Bitcoin reach $100k?",
            category="crypto",
        )
        matches = self.engine.find_matches([a], [b])
        assert len(matches) == 1
        assert matches[0].confidence > 0.8

    def test_similar_titles_moderate_confidence(self):
        a = make_market(
            venue=VenueType.POLYMARKET,
            title="Will the Fed cut rates in March 2026?",
            category="economics",
        )
        b = make_market(
            venue=VenueType.KALSHI,
            title="Fed to cut rates at March 2026 meeting?",
            category="economics",
        )
        matches = self.engine.find_matches([a], [b])
        assert len(matches) == 1
        assert matches[0].confidence > 0.5

    def test_different_categories_no_match(self):
        a = make_market(
            venue=VenueType.POLYMARKET,
            title="Bitcoin above $100k?",
            category="crypto",
        )
        b = make_market(
            venue=VenueType.KALSHI,
            title="Bitcoin above $100k?",
            category="sports",  # Wrong category
        )
        matches = self.engine.find_matches([a], [b])
        assert len(matches) == 0

    def test_unrelated_markets_no_match(self):
        a = make_market(
            venue=VenueType.POLYMARKET,
            title="Will it snow in Miami tomorrow?",
            category="weather",
        )
        b = make_market(
            venue=VenueType.KALSHI,
            title="Champions League winner 2026?",
            category="weather",
        )
        matches = self.engine.find_matches([a], [b])
        assert len(matches) == 0

    def test_temporal_filter_rejects_distant_dates(self):
        a = make_market(
            venue=VenueType.POLYMARKET,
            title="Bitcoin above $100k?",
            category="crypto",
            expiration_days=5,
        )
        b = make_market(
            venue=VenueType.KALSHI,
            title="Bitcoin above $100k?",
            category="crypto",
            expiration_days=365,  # A year away
        )
        engine = EquivalenceEngine(temporal_window_days=7, min_confidence=0.3)
        matches = engine.find_matches([a], [b])
        # Should have lower confidence or no match due to temporal distance
        if matches:
            assert matches[0].confidence < 0.8

    def test_entity_extraction_numbers(self):
        """Numbers like $100k and percentages should be extracted as entities."""
        entities = self.engine._extract_entities("Bitcoin above $150,000 by December?")
        assert "150000" in entities or "150,000" in entities

    def test_text_similarity_stopword_removal(self):
        """Stopwords should not inflate similarity scores."""
        score = self.engine._text_similarity(
            "Will the thing happen?",
            "Will the other thing happen?",
        )
        # After removing stopwords: {"thing", "happen"} vs {"other", "thing", "happen"}
        assert 0.3 < score < 0.9


# ============================================================
# 4. Routing Tests
# ============================================================

class TestRoutingEngine:

    def setup_method(self):
        self.router = RoutingEngine()

    def test_routes_to_cheaper_venue(self):
        a = make_market(venue=VenueType.POLYMARKET, yes_price=0.55, no_price=0.45, volume=1000000)
        b = make_market(venue=VenueType.KALSHI, yes_price=0.60, no_price=0.40, volume=1000000)
        pair = MarketPair(market_a=a, market_b=b, confidence=0.8, match_method="test")

        order = HypotheticalOrder(market_pair=pair, side=OrderSide.BUY_YES, amount_usd=100)
        decision = self.router.route(order)

        # Cheaper yes price = Polymarket (0.55 < 0.60), and equal spread
        assert decision.selected_venue == "polymarket"

    def test_routing_explanation_not_empty(self):
        a = make_market(venue=VenueType.POLYMARKET)
        b = make_market(venue=VenueType.KALSHI)
        pair = MarketPair(market_a=a, market_b=b, confidence=0.8, match_method="test")

        order = HypotheticalOrder(market_pair=pair, side=OrderSide.BUY_YES, amount_usd=100)
        decision = self.router.route(order)

        assert decision.explanation != ""
        assert decision.selected_venue in decision.explanation

    def test_venue_scores_populated(self):
        a = make_market(venue=VenueType.POLYMARKET)
        b = make_market(venue=VenueType.KALSHI)
        pair = MarketPair(market_a=a, market_b=b, confidence=0.8, match_method="test")

        order = HypotheticalOrder(market_pair=pair, side=OrderSide.BUY_YES, amount_usd=100)
        decision = self.router.route(order)

        assert len(decision.venue_scores) == 2
        for vs in decision.venue_scores:
            assert 0.0 <= vs.total_score <= 1.0

    def test_simulate_orders_generates_both_sides(self):
        a = make_market(venue=VenueType.POLYMARKET)
        b = make_market(venue=VenueType.KALSHI)
        pair = MarketPair(market_a=a, market_b=b, confidence=0.8, match_method="test")

        decisions = self.router.simulate_orders([pair])
        assert len(decisions) == 2  # BUY_YES and BUY_NO

    def test_to_dict_serializable(self):
        a = make_market(venue=VenueType.POLYMARKET)
        b = make_market(venue=VenueType.KALSHI)
        pair = MarketPair(market_a=a, market_b=b, confidence=0.8, match_method="test")

        order = HypotheticalOrder(market_pair=pair, side=OrderSide.BUY_YES, amount_usd=100)
        decision = self.router.route(order)
        d = decision.to_dict()

        assert "selected_venue" in d
        assert "explanation" in d
        assert isinstance(d["venue_scores"], list)


# ============================================================
# 5. Integration Test
# ============================================================

class TestPipelineIntegration:

    def test_full_pipeline(self):
        """End-to-end: fetch → normalize → match → route."""
        pipeline = EquinoxPipeline()
        # Use lower threshold for mock data testing (real API uses 0.55)
        pipeline.matcher = EquivalenceEngine(min_confidence=0.4)
        results = asyncio.run(pipeline.run(market_limit=50))

        assert results["summary"]["polymarket_markets"] > 0
        assert results["summary"]["kalshi_markets"] > 0
        assert results["summary"]["matched_pairs"] > 0
        assert results["summary"]["routing_decisions"] > 0

        # Verify matches have required fields
        for match in results["matches"]:
            assert "confidence" in match
            assert 0 < match["confidence"] <= 1.0
            assert match["market_a"]["venue"] != match["market_b"]["venue"]

        # Verify routing decisions have explanations
        for decision in results["routing_decisions"]:
            assert decision["explanation"] != ""
            assert decision["selected_venue"] in ("polymarket", "kalshi")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
