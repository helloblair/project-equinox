"""
Equivalence Detection Engine

This is the hardest part of Equinox: determining whether two markets on
different venues refer to the same real-world event.

The challenge: Polymarket might call it "Will Bitcoin exceed $100k by end of 2026?"
while Kalshi calls it "Bitcoin above $100,000 on December 31". Same event,
completely different phrasing.

Approach (hybrid, documented as the spec requires):

1. CATEGORY FILTER (fast, coarse)
   - Only compare markets in the same category. A politics market
     will never match a crypto market. This prunes the O(n*m) space.

2. TEMPORAL FILTER
   - Markets must have overlapping or close expiration dates.
   - A "Bitcoin > $100k by March 2026" should not match "Bitcoin > $100k by Dec 2026"

3. TEXT SIMILARITY (fuzzy matching)
   - Tokenize titles, remove stopwords, compute Jaccard similarity
   - Threshold-based: high similarity = candidate match

4. ENTITY EXTRACTION (keyword-based)
   - Extract key entities: numbers (prices, dates), proper nouns (Bitcoin, Fed)
   - Markets must share key entities to match

5. CONFIDENCE SCORING
   - Weighted combination of text similarity + entity overlap + temporal proximity
   - Output: MarketPair with confidence score and explanation

Future improvements (talk-through material):
- Embedding-based matching with sentence transformers
- LLM-assisted disambiguation for edge cases
- Learning from human-confirmed matches to improve thresholds
"""

import re
import logging
from datetime import datetime, timedelta
from typing import Optional

from src.models.market import CanonicalMarket, MarketPair

logger = logging.getLogger(__name__)

# Words that carry no discriminative signal
STOPWORDS = {
    "will", "the", "a", "an", "be", "is", "are", "was", "were", "by", "on",
    "in", "at", "to", "of", "for", "and", "or", "not", "this", "that", "it",
    "from", "with", "as", "but", "if", "than", "before", "after", "above",
    "below", "between", "does", "do", "did", "has", "have", "had", "may",
    "might", "would", "could", "should", "shall", "can", "yes", "no",
}


class EquivalenceEngine:
    """
    Detects equivalent markets across venues using a multi-stage pipeline.
    """

    def __init__(
        self,
        text_similarity_threshold: float = 0.3,
        temporal_window_days: int = 7,
        min_confidence: float = 0.4,
        review_threshold: float = 0.3,
    ):
        self.text_threshold = text_similarity_threshold
        self.temporal_window = timedelta(days=temporal_window_days)
        self.min_confidence = min_confidence
        self.review_threshold = review_threshold  # Matches between review and min are flagged
        self.ambiguous_pairs: list[MarketPair] = []  # Low-confidence matches for human review

    def find_matches(
        self,
        markets_a: list[CanonicalMarket],
        markets_b: list[CanonicalMarket],
    ) -> list[MarketPair]:
        """
        Find equivalent markets between two venue market lists.

        Pipeline:
        1. Group by category → only compare within same category
        2. For each cross-venue pair in same category:
           a. Check temporal proximity
           b. Compute text similarity
           c. Extract and compare entities
           d. Score and threshold

        Matches above min_confidence are returned as confirmed.
        Matches between review_threshold and min_confidence are stored
        in self.ambiguous_pairs for human review (addresses PRD requirement
        to "handle ambiguity" explicitly).
        """
        matches = []
        self.ambiguous_pairs = []

        # Group markets by category
        cats_a = self._group_by_category(markets_a)
        cats_b = self._group_by_category(markets_b)

        # Only compare categories that exist in both venues
        shared_categories = set(cats_a.keys()) & set(cats_b.keys())
        logger.info(f"Shared categories: {shared_categories}")

        for category in shared_categories:
            group_a = cats_a[category]
            group_b = cats_b[category]

            for market_a in group_a:
                for market_b in group_b:
                    pair = self._evaluate_pair(market_a, market_b)
                    if pair is not None:
                        matches.append(pair)

        # Sort by confidence descending
        matches.sort(key=lambda p: p.confidence, reverse=True)

        logger.info(
            f"Found {len(matches)} confirmed matches (>{self.min_confidence:.0%}), "
            f"{len(self.ambiguous_pairs)} ambiguous matches ({self.review_threshold:.0%}–{self.min_confidence:.0%}, flagged for review)"
        )
        return matches

    def _evaluate_pair(
        self,
        market_a: CanonicalMarket,
        market_b: CanonicalMarket,
    ) -> Optional[MarketPair]:
        """
        Evaluate whether two markets refer to the same event.
        Returns a MarketPair if confidence exceeds threshold, else None.
        """
        details = {}

        # Stage 1: Temporal filter
        temporal_score = self._temporal_score(market_a, market_b)
        details["temporal_score"] = temporal_score
        if temporal_score == 0.0 and (market_a.expiration_date and market_b.expiration_date):
            return None  # Expiration dates too far apart

        # Stage 2: Text similarity
        text_score = self._text_similarity(market_a.title, market_b.title)
        details["text_score"] = text_score

        # Stage 3: Entity overlap
        entity_score = self._entity_overlap(market_a.title, market_b.title)
        details["entity_score"] = entity_score

        # Stage 4: Description similarity (bonus)
        desc_score = self._text_similarity(market_a.description, market_b.description)
        details["description_score"] = desc_score

        # Weighted confidence score
        confidence = (
            text_score * 0.35
            + entity_score * 0.35
            + temporal_score * 0.20
            + desc_score * 0.10
        )
        details["confidence_breakdown"] = {
            "text_weight": 0.35,
            "entity_weight": 0.35,
            "temporal_weight": 0.20,
            "description_weight": 0.10,
        }

        if confidence < self.review_threshold:
            return None  # Below review threshold — discard entirely

        if confidence < self.min_confidence:
            # Between review and confirmed thresholds — flag for human review
            ambiguous_pair = MarketPair(
                market_a=market_a,
                market_b=market_b,
                confidence=round(confidence, 4),
                match_method="ambiguous_needs_review",
                match_details=details,
            )
            self.ambiguous_pairs.append(ambiguous_pair)
            return None

        # Determine match method label
        if entity_score > 0.6 and text_score > 0.5:
            method = "strong_text_and_entity"
        elif entity_score > 0.6:
            method = "entity_overlap"
        elif text_score > 0.5:
            method = "fuzzy_title"
        else:
            method = "weak_composite"

        return MarketPair(
            market_a=market_a,
            market_b=market_b,
            confidence=round(confidence, 4),
            match_method=method,
            match_details=details,
        )

    def _text_similarity(self, text_a: str, text_b: str) -> float:
        """
        Jaccard similarity on tokenized, cleaned text.

        Jaccard = |intersection| / |union| of token sets.
        Simple but effective for this use case because prediction market
        titles tend to share key terms when they reference the same event.
        """
        tokens_a = self._tokenize(text_a)
        tokens_b = self._tokenize(text_b)

        if not tokens_a or not tokens_b:
            return 0.0

        intersection = tokens_a & tokens_b
        union = tokens_a | tokens_b

        return len(intersection) / len(union) if union else 0.0

    def _entity_overlap(self, text_a: str, text_b: str) -> float:
        """
        Extract and compare named entities / key terms.

        We focus on:
        - Numbers (prices, percentages, dates)
        - Capitalized proper nouns (Bitcoin, Fed, Trump)
        - Key financial/political terms
        """
        entities_a = self._extract_entities(text_a)
        entities_b = self._extract_entities(text_b)

        if not entities_a or not entities_b:
            return 0.0

        intersection = entities_a & entities_b
        # Use min-set overlap (Overlap coefficient) instead of Jaccard
        # because one venue might mention more context than the other
        min_size = min(len(entities_a), len(entities_b))

        return len(intersection) / min_size if min_size > 0 else 0.0

    def _temporal_score(self, market_a: CanonicalMarket, market_b: CanonicalMarket) -> float:
        """
        Score based on how close the expiration dates are.
        Returns 1.0 for same day, decays to 0.0 beyond temporal_window.
        """
        if market_a.expiration_date is None or market_b.expiration_date is None:
            return 0.5  # Unknown — don't penalize but don't reward

        delta = abs((market_a.expiration_date - market_b.expiration_date).total_seconds())
        window_seconds = self.temporal_window.total_seconds()

        if delta == 0:
            return 1.0
        elif delta > window_seconds:
            return 0.0
        else:
            return 1.0 - (delta / window_seconds)

    def _tokenize(self, text: str) -> set[str]:
        """Lowercase, remove punctuation, filter stopwords."""
        text = text.lower()
        text = re.sub(r'[^\w\s]', ' ', text)
        tokens = text.split()
        return {t for t in tokens if t not in STOPWORDS and len(t) > 1}

    def _extract_entities(self, text: str) -> set[str]:
        """
        Extract key entities from market title text.

        Entities include:
        - Numbers (with optional $ or % prefix/suffix)
        - Capitalized words (proper nouns)
        - Known key terms
        """
        entities = set()

        # Numbers (including $100k, 100%, 3.5%, $100,000)
        numbers = re.findall(r'[\$]?\d[\d,]*\.?\d*[k|K|M|B|%]?', text)
        for n in numbers:
            # Normalize: remove $, commas; lowercase k/M/B
            normalized = n.replace("$", "").replace(",", "").lower()
            entities.add(normalized)

        # Proper nouns — words that start with uppercase (skip first word of sentence)
        words = text.split()
        for i, word in enumerate(words):
            clean = re.sub(r'[^\w]', '', word)
            if clean and clean[0].isupper() and clean.lower() not in STOPWORDS:
                entities.add(clean.lower())

        return entities

    def _group_by_category(self, markets: list[CanonicalMarket]) -> dict[str, list[CanonicalMarket]]:
        """Group markets by their normalized category."""
        groups: dict[str, list[CanonicalMarket]] = {}
        for market in markets:
            cat = market.category.lower()
            if cat not in groups:
                groups[cat] = []
            groups[cat].append(market)
        return groups
