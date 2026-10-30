"""Storage projector implementation for accessing nested data."""

from typing import Any

from fxtr.object_model import core


class StorageHandle_Project(core.StorageHandle[Any]):
    """Implementation of a storage projector that extracts data using an access path."""

    interface_spec = core.INFER_INTERFACE_FROM_BASE_CLASS

    def __init__(
        self,
        resolver: core.PlanResolver,
        /,
        *,
        handle: core.Plan[core.StorageHandle[Any]],
        access_path: tuple[tuple[str, Any], ...],
    ):
        """Initialize the storage projector.

        Args:
            resolver: Plan resolver to get dependencies
            handle: The source storage handle to project from
            access_path: The access path to follow to extract the data
        """
        self._access_path = access_path

        # Get the source handle from dependencies
        self._source_handle = resolver(handle)

    async def retrieve(self) -> Any:
        """Get the projected data by following the access path."""
        # Get the source data
        source_impl = self._source_handle
        data = await source_impl.retrieve()

        # Follow the access path
        current = data
        for step in self._access_path:
            match step:
                case ("attr", attr_name):
                    assert isinstance(attr_name, str)
                    current = getattr(current, attr_name)
                case ("item", key):
                    current = current[key]
                case _:
                    raise ValueError(f"Unsupported access path step type: {type(step)}")

        return current
