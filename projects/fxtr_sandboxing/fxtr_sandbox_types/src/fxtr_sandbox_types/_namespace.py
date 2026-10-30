"""UUID namespace for `system_entities.define_type` calls in this package.

Held centrally so every entity / protocol type defined here lands under one
namespace, and isn't accidentally collided with by other packages.
"""

import uuid

# Random UUIDv4. Specific to fxtr_sandbox_types and never reused for anything else.
SHARED_SANDBOX_NS = uuid.UUID("4d8f2c6e-7a91-4f3b-bc5e-1a2b3c4d5e6f")
