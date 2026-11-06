"""Every tool result reaches the client as text and nothing else.

`mcp` turns a `-> str` tool into structured output by default: it declares an
`outputSchema` and sends the same JSON again, escaped, as `structuredContent`.
A client that shows the model the structured copy instead of the text gives
it a reply longer by the `{"result": ...}` wrapper and by one character for
every quotation mark and backslash. Asserted through the registered server,
because that is where the result is serialised.
"""

import asyncio
import json

import monzo_mcp.cli  # noqa: F401 - registers the tools exactly as production does
from monzo_mcp.mcp_instance import mcp

_TOOLS = asyncio.run(mcp.list_tools())


def _arguments(tool):
    """Placeholder values for a tool's required arguments, all strings."""
    args = {}
    for name in tool.input_schema.get("required", []):
        kind = tool.input_schema["properties"][name].get("type")
        assert kind == "string", f"{tool.name} requires {name!r} of type {kind!r}; add a value"
        args[name] = "anything"
    return args


def test_the_server_registers_tools():
    assert len(_TOOLS) > 0, "no tools registered, so the tests below would check nothing"


def test_no_tool_declares_an_output_schema():
    assert _TOOLS
    declared = sorted(tool.name for tool in _TOOLS if tool.output_schema is not None)
    assert declared == [], f"these tools declare an outputSchema: {declared}"


def test_no_tool_result_carries_structured_content():
    # With no credentials, which the suite's fixture guarantees, every tool
    # answers through the credential gate without touching the API or cache.
    assert _TOOLS
    for tool in _TOOLS:
        result = asyncio.run(mcp.call_tool(tool.name, _arguments(tool)))
        assert result.structured_content is None, f"{tool.name} sends structuredContent"
        text = "".join(c.text for c in result.content if getattr(c, "text", None))
        assert "monzo-mcp auth" in json.loads(text)["error"], f"{tool.name}: {text!r}"
