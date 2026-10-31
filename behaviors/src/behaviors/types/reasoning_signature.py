"""The codec for OpenAI Responses reasoning identity inside a signature blob.

``ContentSummarizedReasoning.signature`` is an opaque blob echoed back to the
provider verbatim. For the OpenAI Responses API the echo needs two values —
the reasoning item's ``id`` and its ``encrypted_content``, which the API
verifies against each other — so everything that ingests a Responses reasoning
item packs both into the one slot as ``{item_id}:{payload}``, and everything
that renders one back unpacks it. The colon is unambiguous: it appears in
neither item ids nor base64 payloads.

A blob with no packed id (no ``rs_``-prefixed colon prefix) is some other
provider's signature travelling through a Responses-shaped hop. It is wrapped
in a synthesized ``rs_behaviors...`` item there, and the synthetic id is
elided again on re-encode so the original blob survives byte-identical.
"""

SYNTHETIC_REASONING_ID_PREFIX = "rs_behaviors"
"""Prefix of item ids synthesized for blobs that carry no id of their own."""


def encode_reasoning_signature(item_id: str, encrypted_content: str) -> str:
    """Pack a reasoning item's identity into one opaque signature blob.

    A synthetic or missing id is not packed: the payload was a foreign blob
    the item merely wrapped, and is restored verbatim.
    """
    if not item_id.startswith("rs_") or item_id.startswith(SYNTHETIC_REASONING_ID_PREFIX):
        return encrypted_content
    return f"{item_id}:{encrypted_content}"


def decode_reasoning_signature(signature: str) -> tuple[str | None, str]:
    """The (item id, encrypted payload) packed in a signature blob.

    The id is None for a blob without one — a foreign provider's signature.
    """
    item_id, separator, payload = signature.partition(":")
    if separator and item_id.startswith("rs_"):
        return item_id, payload
    return None, signature
