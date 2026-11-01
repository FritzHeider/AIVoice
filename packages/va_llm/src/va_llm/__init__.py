"""VoiceAgents LLM gateway (specs/10-llm-gateway.md): providers, call policies, breakers, metering."""

from va_llm.breaker import BreakerBoard, BreakerState, CircuitBreaker
from va_llm.clients import CALL_ID_HEADER, LlmClients
from va_llm.dialogue import DialogueRequest, DialogueStream
from va_llm.embeddings import Embeddings
from va_llm.errors import (
    LlmConnectionError,
    LlmError,
    LlmHttpError,
    LlmProtocolError,
    LlmRateLimitedError,
    LlmTimeoutError,
    LlmUnavailableError,
    OrgProviderError,
    StructuredOutputError,
)
from va_llm.gateway import LlmGateway
from va_llm.metering import EmbeddingUsageRecord, EmbeddingUsageRecorder, UsageRecord, UsageRecorder
from va_llm.metrics import LlmMetrics
from va_llm.models import ChatMessage, Completion, ForceTool, TokenUsage, ToolCall, ToolChoice, ToolDef
from va_llm.org_providers import OrgProviderStore, encrypt_org_api_key
from va_llm.policy import POLICIES, CallPolicy, CallType
from va_llm.pricing import MODEL_COSTS, CostTable, ModelCost, load_costs
from va_llm.probe import ProbeResult, probe_provider
from va_llm.providers import LlmProvider, ProviderRegistry, platform_provider
from va_llm.settings import LlmSettings
from va_llm.structured import TOOL_NAMES, register_structured_function, registered_schema, tool_schema

__all__ = [
    "CALL_ID_HEADER",
    "MODEL_COSTS",
    "POLICIES",
    "TOOL_NAMES",
    "BreakerBoard",
    "BreakerState",
    "CallPolicy",
    "CallType",
    "ChatMessage",
    "CircuitBreaker",
    "Completion",
    "CostTable",
    "DialogueRequest",
    "DialogueStream",
    "EmbeddingUsageRecord",
    "EmbeddingUsageRecorder",
    "Embeddings",
    "ForceTool",
    "LlmClients",
    "LlmConnectionError",
    "LlmError",
    "LlmGateway",
    "LlmHttpError",
    "LlmMetrics",
    "LlmProtocolError",
    "LlmProvider",
    "LlmRateLimitedError",
    "LlmSettings",
    "LlmTimeoutError",
    "LlmUnavailableError",
    "ModelCost",
    "OrgProviderError",
    "OrgProviderStore",
    "ProbeResult",
    "ProviderRegistry",
    "StructuredOutputError",
    "TokenUsage",
    "ToolCall",
    "ToolChoice",
    "ToolDef",
    "UsageRecord",
    "UsageRecorder",
    "encrypt_org_api_key",
    "load_costs",
    "platform_provider",
    "probe_provider",
    "register_structured_function",
    "registered_schema",
    "tool_schema",
]
