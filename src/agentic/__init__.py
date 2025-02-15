"""
Agentic - A Python library for building powerful LLM-based agents
"""

__version__ = "0.1.0"

from .agent import LlmAgent
from .client import (
    LlmClient,
    GeminiProvider,
    AnthropicProvider,
    OpenAiProvider,
    FireworksProvider,
)
from .models import (
    Message,
    ToolCall,
    Usage,
    LlmGenerateStructuredResponse,
    LlmResponseMetadata,
)

__all__ = [
    "LlmAgent",
    "LlmClient",
    "GeminiProvider",
    "AnthropicProvider",
    "OpenAiProvider",
    "FireworksProvider",
    "Message",
    "ToolCall",
    "Usage",
    "LlmGenerateStructuredResponse",
    "LlmResponseMetadata",
]
