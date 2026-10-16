from .providers import (
    AgentResult,
    DeepSeekAgent,
    call_tool,
    openai_compatible_tools,
    parse_arguments,
)

_call_tool = call_tool
_deepseek_tools = openai_compatible_tools
_parse_arguments = parse_arguments

__all__ = [
    "AgentResult",
    "DeepSeekAgent",
    "_call_tool",
    "_deepseek_tools",
    "_parse_arguments",
]
