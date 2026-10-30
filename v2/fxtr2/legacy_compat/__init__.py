"""Legacy compatibility module for fxtr v1 entities."""

from fxtr2.legacy_compat.data import (
    LEGACY_V1_TYPE_ID,
    LegacyCompat,
    PostOrderEntityCollector,
    walk_and_collect_cids,
    walk_and_collect_fxtr_ids,
    walk_and_replace_cids,
    walk_and_replace_fxtr_ids,
)

__all__ = [
    "LEGACY_V1_TYPE_ID",
    "LegacyCompat",
    "PostOrderEntityCollector",
    "walk_and_collect_cids",
    "walk_and_collect_fxtr_ids",
    "walk_and_replace_cids",
    "walk_and_replace_fxtr_ids",
]
