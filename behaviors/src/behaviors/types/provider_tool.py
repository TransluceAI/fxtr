"""Provider-side tools: tools a provider executes inside the model call.

Web search, web fetch, code execution, file search, image generation and hosted MCP connectors
are declared in a request's tool list like any other tool, but the provider runs them itself and
feeds their results into the same response. The model vocabulary has no slot for them (a
`ToolParam` is a tool the harness runs), and they matter to whoever sandboxes a harness for
another reason: a provider-side tool reaches the network from the provider, so the sandbox's
egress allowlist does not govern it. Each harness-side codec reports the ones a request declares,
by family, so a policy can allow families explicitly and never an unknown kind.
"""

from dataclasses import dataclass
from typing import Literal

ProviderToolFamily = Literal[
    "web_search",
    "web_fetch",
    "code_execution",
    "file_search",
    "image_generation",
    "mcp",
    "unknown",
]
"""What a provider-side tool does, across dialects.

`web_search`: Anthropic `web_search_*`, Responses `web_search*`, Gemini `googleSearch`.
`web_fetch`: Anthropic `web_fetch_*`, Gemini `urlContext`. `code_execution`: Anthropic
`code_execution_*`, Responses `code_interpreter`, Gemini `codeExecution`. `file_search`: Responses
`file_search`, Gemini `fileSearch`. `image_generation`: Responses `image_generation`. `mcp`: a
hosted MCP connector (Anthropic `mcp_servers` / `mcp_toolset`, Responses `mcp`). `unknown`: a
tool type the codec does not recognize as one the harness runs; a policy cannot allow it.

Not families: tools the API defines but the harness executes (computer use, Anthropic's bash and
text editor, Responses `local_shell`), and the providers' tool-search tools, which run at the
provider but only over the request's own tool definitions, with no reach beyond it.
"""

ALLOWABLE_PROVIDER_TOOL_FAMILIES: tuple[ProviderToolFamily, ...] = (
    "web_search",
    "web_fetch",
    "code_execution",
    "file_search",
    "image_generation",
    "mcp",
)
"""The families a run's policy may allow (`unknown` is not one)."""


@dataclass(frozen=True)
class ProviderTool:
    """One provider-side tool a request declares.

    Attributes:
        family: What it does.
        name: The dialect's identifier for it, for messages: the tool's `type` or `name` on the
            wire, or for a hosted MCP connector the server's name.
    """

    family: ProviderToolFamily
    name: str
