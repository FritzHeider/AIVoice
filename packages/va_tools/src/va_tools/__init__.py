"""VoiceAgents tool runtime (specs/13): the SSRF-safe HTTP executor and JSONPath extraction.

``va_tools`` depends on ``va_engine`` (templating, the :mod:`va_engine.ports` protocols), never the reverse.
"""

from va_tools.http import (
    CONNECT_TIMEOUT_S,
    MAX_RESPONSE_BYTES,
    USER_AGENT,
    HttpExecutor,
    open_http_executor,
)
from va_tools.jsonpath import InvalidJsonPathError, compile_path, extract, extract_all

__all__ = [
    "CONNECT_TIMEOUT_S",
    "MAX_RESPONSE_BYTES",
    "USER_AGENT",
    "HttpExecutor",
    "InvalidJsonPathError",
    "compile_path",
    "extract",
    "extract_all",
    "open_http_executor",
]
