# SPDX-FileCopyrightText: 2026 RyanLin-InfEvo
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Hyperliquid API client, streaming leaderboard, rate limiter, and normalization."""

from hyperpod.client.info_api import (
    DEFAULT_INFO_URL,
    ApiHttpError,
    ApiTransportError,
    FillsPageResult,
    FundingPageResult,
    InfoApiClient,
    InfoApiError,
    NormalizedFill,
    aggregate_public_fill_rows,
    compute_account_equity,
    is_main_perp_coin,
    normalize_fills,
)
from hyperpod.client.leaderboard import (
    DEFAULT_LEADERBOARD_URL,
    DEFAULT_MAX_LEADERBOARD_BYTES,
    LeaderboardClient,
    LeaderboardFetchError,
    LeaderboardFetchResult,
    fetch_leaderboard,
)
from hyperpod.client.ratelimit import (
    RateLimitStats,
    WeightRateLimiter,
    base_weight_for_endpoint,
    compute_dynamic_extra_weight,
)

__all__ = [
    "ApiHttpError",
    "ApiTransportError",
    "DEFAULT_INFO_URL",
    "DEFAULT_LEADERBOARD_URL",
    "DEFAULT_MAX_LEADERBOARD_BYTES",
    "FillsPageResult",
    "FundingPageResult",
    "InfoApiClient",
    "InfoApiError",
    "LeaderboardClient",
    "LeaderboardFetchError",
    "LeaderboardFetchResult",
    "NormalizedFill",
    "RateLimitStats",
    "WeightRateLimiter",
    "aggregate_public_fill_rows",
    "base_weight_for_endpoint",
    "compute_account_equity",
    "compute_dynamic_extra_weight",
    "fetch_leaderboard",
    "is_main_perp_coin",
    "normalize_fills",
]
