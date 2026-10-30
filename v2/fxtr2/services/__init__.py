"""User-facing service-capability layer.

Core modules:

- :mod:`fxtr2.services.service_cap_common` — wire shape, packet
  build/dispatch/return-resource code, the ``@service_method``
  marker + ``walk_service_methods`` discovery helper, and the
  :class:`~fxtr2.services.service_cap_common.CommonServiceImpl`
  ABC shared by every service implementation strategy.
- :mod:`fxtr2.services.untyped_service` — the dynamic-vocabulary
  :class:`UntypedServiceImpl` / :class:`UnknownServiceProxy` pair,
  plus the :class:`SimpleAsyncServiceImpl` ABC that owns
  ``@service_method`` dispatch.
- :mod:`fxtr2.services.service_dialect` — the typed-service
  :class:`FxtrServicesDefaultDialect` (mashumaro Dialect) plus
  per-type strategies (``EntityRef[T]``, ``ServiceRef[T]``,
  ``FxtrID``, ``frozenset``, ``set``, ``frozendict``) and the
  ``(annotation, dialect)`` codec cache.
- :mod:`fxtr2.services.typed_service` — the ``@service_protocol``
  decorator, :class:`TypedServiceImpl` ABC, and the dynamic
  proxy-class / wrapper-dataclass machinery for typed services.

Plus the explicit-subscription layer built on top:

- :mod:`fxtr2.services.subscription_types` — the ``Subscriber`` /
  ``UnsubscribeHandle`` / ``EventSource`` protocol primitives.
- :mod:`fxtr2.services.subscription_util` — ``SubscriberHub``, the
  reusable broadcast helper for implementing the source side.

See ``LOCAL_CODE_OVERVIEW.md`` for orientation.
"""
