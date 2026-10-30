"""Shared namespace UUID for workflow entity/service type ids.

Kept in its own leaf module so ``handles`` / ``task_types`` /
``stateless_service_provider`` can all define types without import cycles.
"""

from __future__ import annotations

import uuid

#: Namespace UUID for all workflow-system entity and service type ids.
WORKFLOWS_NS = uuid.UUID("a7f3c2d8-5e14-4b9a-9c3f-2d8e6a1b4c70")
