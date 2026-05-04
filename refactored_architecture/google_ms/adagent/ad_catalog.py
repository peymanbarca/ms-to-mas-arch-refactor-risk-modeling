"""
adservice/ad_catalog.py

Direct Python port of the Java AdService's static ads map.

Java original used ImmutableListMultimap<String, Ad> (newer version) and
HashMap<String, Ad> (original census-ecosystem version).

This module reproduces both the category→list mapping (newer) AND the
per-product-key mapping (original), so the servicer can use either strategy.

Each entry mirrors the exact product IDs, URLs, and ad copy from the Java source.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from collections import defaultdict
import random


@dataclass(frozen=True)
class AdEntry:
    """Lightweight in-process equivalent of the proto Ad message."""
    redirect_url: str
    text: str


# ── Full catalog (mirrors Java createAdsMap + initializeAds) ─────────────────
#
# Format: category → list of AdEntry
# Matches the ImmutableListMultimap used in the current main-branch Java code.
#
# Product IDs are the real IDs from the Online Boutique JSON catalog.

_RAW: list[tuple[str, AdEntry]] = [
    # personal_care
    ("personal_care", AdEntry(
        redirect_url="/product/2ZYFJ3GM2N",
        text="Hairdryer for sale. 50% off.",
    )),
    # apparel / clothing
    ("apparel", AdEntry(
        redirect_url="/product/66VCHSJNUP",
        text="Tank top for sale. 20% off.",
    )),
    # decor / home
    ("decor", AdEntry(
        redirect_url="/product/0PUK6V6EV0",
        text="Candle holder for sale. 30% off.",
    )),
    # kitchen
    ("kitchen", AdEntry(
        redirect_url="/product/9SIQT8TOJO",
        text="Bamboo glass jar for sale. 10% off.",
    )),
    # kitchen (second ad, same category – multimap semantics)
    ("kitchen", AdEntry(
        redirect_url="/product/1YMWWN1N4O",
        text="Home Barista kitchen kit for sale. Buy one, get second kit for free.",
    )),
    # vintage / clothing
    ("vintage", AdEntry(
        redirect_url="/product/L9ECAV7KIM",
        text="Vintage typewriter for sale. Free shipping.",
    )),
    # music / accessories
    ("music", AdEntry(
        redirect_url="/product/2ZYFJ3GM2N",
        text="Vintage record player for sale. 30% off.",
    )),
    # cycling / outdoors
    ("cycling", AdEntry(
        redirect_url="/product/9SIQT8TOJO",
        text="City Bike for sale. 10% off.",
    )),
    # photography
    ("photography", AdEntry(
        redirect_url="/product/OLJCESPC7Z",
        text="Film camera for sale. 50% off.",
    )),
]

# category → [AdEntry, ...]  (mirrors ImmutableListMultimap)
ADS_BY_CATEGORY: dict[str, list[AdEntry]] = defaultdict(list)
for _cat, _ad in _RAW:
    ADS_BY_CATEGORY[_cat].append(_ad)

# key → AdEntry  (mirrors the older HashMap<String,Ad> used in initializeAds)
ADS_BY_KEY: dict[str, AdEntry] = {
    "camera":     AdEntry("/product/2ZYFJ3GM2N", "Film camera for sale. 50% off."),
    "bike":       AdEntry("/product/9SIQT8TOJO", "City Bike for sale. 10% off."),
    "kitchen":    AdEntry("/product/1YMWWN1N4O",
                          "Home Barista kitchen kit for sale. Buy one, get second kit for free."),
    "hair":       AdEntry("/product/2ZYFJ3GM2N", "Hairdryer for sale. 50% off."),
    "tank":       AdEntry("/product/66VCHSJNUP", "Tank top for sale. 20% off."),
    "candle":     AdEntry("/product/0PUK6V6EV0", "Candle holder for sale. 30% off."),
    "bamboo":     AdEntry("/product/9SIQT8TOJO", "Bamboo glass jar for sale. 10% off."),
    "typewriter": AdEntry("/product/L9ECAV7KIM", "Vintage typewriter for sale. Free shipping."),
    "record":     AdEntry("/product/2ZYFJ3GM2N",  "Vintage record player for sale. 30% off."),
}

# All ads flattened – used by getDefaultAds / getRandomAds
ALL_ADS: list[AdEntry] = [ad for ads in ADS_BY_CATEGORY.values() for ad in ads]

MAX_ADS_TO_SERVE: int = 2


# ── lookup helpers (mirror the Java instance methods) ────────────────────────

def get_ads_by_category(category: str) -> list[AdEntry]:
    """
    Java: Collection<Ad> getAdsByCategory(String category)
    Returns the list of ads registered under *category*, or [] if none.
    """
    return list(ADS_BY_CATEGORY.get(category, []))


def get_ads_by_key(key: str) -> AdEntry | None:
    """
    Java: Ad getAdsByKey(String key)
    Single-ad lookup used by the original census-ecosystem HashMap strategy.
    """
    return ADS_BY_KEY.get(key)


def get_random_ads(n: int = MAX_ADS_TO_SERVE) -> list[AdEntry]:
    """
    Java: List<Ad> getDefaultAds()  /  getRandomAds()
    Returns *n* randomly selected ads (with replacement) from ALL_ADS.
    """
    if not ALL_ADS:
        return []
    return [random.choice(ALL_ADS) for _ in range(n)]