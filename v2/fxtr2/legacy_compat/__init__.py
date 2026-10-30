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
from fxtr2.legacy_compat.runset_conversion import convert_legacy_runset_to_run_cache

__all__ = [
    "LEGACY_V1_TYPE_ID",
    "LegacyCompat",
    "PostOrderEntityCollector",
    "convert_legacy_runset_to_run_cache",
    "walk_and_collect_cids",
    "walk_and_collect_fxtr_ids",
    "walk_and_replace_cids",
    "walk_and_replace_fxtr_ids",
]
