"""Abstract base class for read-only fxtr namespaces.

FxtrReadNamespace provides the read-only subset of FxtrNamespace's interface:
retrieve, try_retrieve, and list_pins. This allows code to be written against
the read-only interface and accept either a full namespace or a read-only one.
"""

from __future__ import annotations

import abc
import typing

from fxtr2.data import entity_ref_scope
from fxtr2.data.entity_core import FxtrID


class FxtrReadNamespace(abc.ABC):
    """Abstract base class for read-only fxtr namespaces."""

    @property
    @abc.abstractmethod
    def scope(self) -> entity_ref_scope.FxtrScope:
        """Get the namespace's scope."""
        ...

    def _resolve_into_scope(
        self,
        into_scope: entity_ref_scope.FxtrScope | None,
        copy_into_scope: bool,
        method_name: str,
    ) -> entity_ref_scope.FxtrScope:
        """Resolve + validate the target scope for ``retrieve`` / ``try_retrieve``.

        Mirrors ``FxtrNamespace._resolve_into_scope`` via the shared
        ``resolve_retrieve_into_scope`` helper: ``into_scope=None`` resolves to
        the active scope, and the lift path requires the namespace's scope to be
        included in the target (else a helpful ``ValueError``). Subclass
        ``_retrieve_impl``s call this so the documented "If None, the active
        scope" contract is honored uniformly.
        """
        return entity_ref_scope.resolve_retrieve_into_scope(
            self.scope,
            into_scope,
            copy_into_scope,
            type_name=type(self).__name__,
            method_name=method_name,
        )

    @typing.overload
    async def retrieve(
        self,
        key: str,
        type: None = None,
        into_scope: entity_ref_scope.FxtrScope | None = None,
        copy_into_scope: bool = False,
    ) -> FxtrID: ...

    @typing.overload
    async def retrieve[
        T: entity_ref_scope.Entity
    ](
        self,
        key: str,
        type: type[T],
        into_scope: entity_ref_scope.FxtrScope | None = None,
        copy_into_scope: bool = False,
    ) -> entity_ref_scope.EntityRef[T]: ...

    async def retrieve[
        T: entity_ref_scope.Entity
    ](
        self,
        key: str,
        type: type[T] | None = None,
        into_scope: entity_ref_scope.FxtrScope | None = None,
        copy_into_scope: bool = False,
    ) -> (FxtrID | entity_ref_scope.EntityRef[T]):
        """Retrieve an entity from the namespace with a given key.

        Args:
            key: The key to retrieve the entity from.
            type: The expected type of the entity. If None, returns a raw FxtrID.
            into_scope: The scope to retrieve the entity into. If None, the active scope.
            copy_into_scope: If True, copy into the given scope instead of lifting.

        Returns:
            EntityRef[T] if type is provided, otherwise FxtrID.

        Raises:
            KeyError: If the pin is not found.
        """
        result = await self._retrieve_impl(
            key, type, into_scope, copy_into_scope, allow_not_found=False
        )
        assert result is not None
        return result

    @typing.overload
    async def try_retrieve(
        self,
        key: str,
        type: None = None,
        into_scope: entity_ref_scope.FxtrScope | None = None,
        copy_into_scope: bool = False,
    ) -> FxtrID | None: ...

    @typing.overload
    async def try_retrieve[
        T: entity_ref_scope.Entity
    ](
        self,
        key: str,
        type: type[T],
        into_scope: entity_ref_scope.FxtrScope | None = None,
        copy_into_scope: bool = False,
    ) -> (entity_ref_scope.EntityRef[T] | None): ...

    async def try_retrieve[
        T: entity_ref_scope.Entity
    ](
        self,
        key: str,
        type: type[T] | None = None,
        into_scope: entity_ref_scope.FxtrScope | None = None,
        copy_into_scope: bool = False,
    ) -> (FxtrID | entity_ref_scope.EntityRef[T] | None):
        """Retrieve an entity, or None if not found.

        Same as retrieve, but returns None instead of raising KeyError.
        """
        return await self._retrieve_impl(
            key, type, into_scope, copy_into_scope, allow_not_found=True
        )

    @abc.abstractmethod
    async def _retrieve_impl[
        T: entity_ref_scope.Entity
    ](
        self,
        key: str,
        type: type[T] | None,
        into_scope: entity_ref_scope.FxtrScope | None,
        copy_into_scope: bool,
        allow_not_found: bool,
    ) -> (FxtrID | entity_ref_scope.EntityRef[T] | None):
        """Implementation of retrieve/try_retrieve. Subclasses must override."""
        ...

    @abc.abstractmethod
    async def list_pins(self, prefix: str = "") -> dict[str, FxtrID]:
        """List all pins in the namespace, optionally filtered by prefix.

        Args:
            prefix: Only return pins whose keys start with this prefix.

        Returns:
            Dict mapping pin names to FxtrIDs.
        """
        ...
