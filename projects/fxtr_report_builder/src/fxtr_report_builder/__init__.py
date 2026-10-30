"""Public package surface.

Entity types, low-level MDX builder, and high-level
report-construction helpers.
"""

from fxtr_report_builder._ts_dist_path import MdxSubprocessError
from fxtr_report_builder.entities import (
    ROOT_REPORT_TYPE_ID,
    SUB_REPORT_TYPE_ID,
    RootReport,
    SubReport,
)
from fxtr_report_builder.mdx_builder import (
    MdxBuildError,
    build_mdx_tree,
    build_root_report,
    build_sub_report,
)

__all__ = [
    "ROOT_REPORT_TYPE_ID",
    "SUB_REPORT_TYPE_ID",
    "MdxBuildError",
    "MdxSubprocessError",
    "RootReport",
    "SubReport",
    "build_mdx_tree",
    "build_root_report",
    "build_sub_report",
]
