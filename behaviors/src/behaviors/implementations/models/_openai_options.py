"""The request options the OpenAI adapters send with: model-endpoint authentication only.

The OpenAI SDK chooses a request's credential by the endpoint's declared security. Its model
methods (`chat.completions.create`, `responses.create`) allow only the model API key, while a
generic request's default also allows the admin key. The shared transport builds generic requests
(`_raw_stream._send`), so the OpenAI adapters declare the model endpoints' security themselves:
a client holding only an admin key sends no credential rather than sending that key.
"""

from typing import Any

from openai._models import FinalRequestOptions, SecurityOptions


class ModelEndpointOptions:
    """`FinalRequestOptions` as the transport constructs them, with model-endpoint security."""

    @staticmethod
    def construct(**fields: Any) -> Any:
        return FinalRequestOptions.construct(  # pyright: ignore[reportDeprecated]
            **fields, security=SecurityOptions(bearer_auth=True)
        )
