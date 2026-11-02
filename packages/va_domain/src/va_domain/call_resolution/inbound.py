"""The request layer of an inbound call (specs/16-phone-numbers.md §4, specs/06 §7 step 8; Decision P05-T12a).

The number's `InboundConfig` plays the Send Call request body: only its set keys become request parameters,
`prompt` becomes `task`, the caller is `phone_number` and the called number is `from`. A number without a
runner answers with the org's `default_inbound_prompt`.
"""

from __future__ import annotations

from typing import Final

from pydantic import ValidationError

from va_domain.calls import CallRequestError, SendCallRequest
from va_domain.numbers import ROUTING_FIELDS, InboundConfig
from va_domain.org import OrgSettings, field_path

__all__ = ["ORG_NAME_VARIABLE", "default_inbound_prompt", "inbound_request"]

ORG_NAME_VARIABLE: Final = "{{org_name}}"
_RUNNER_FIELDS: Final = ("prompt", "pathway_id", "persona_id", "agent_id")


def default_inbound_prompt(org: OrgSettings, org_name: str) -> str:
    """The org `default_inbound_prompt` with `{{org_name}}` filled in; other variables render per call."""
    return org.default_inbound_prompt.replace(ORG_NAME_VARIABLE, org_name)


def inbound_request(
    config: InboundConfig, *, caller: str, called: str, org: OrgSettings, org_name: str
) -> SendCallRequest:
    """The Send Call request of an inbound call from `caller` to `called` (raises `CallRequestError`).

    `caller` is kept as Telnyx sent it (`anonymous` included); the loader normalizes it.
    """
    body = config.model_dump(exclude_unset=True, exclude=set(ROUTING_FIELDS), by_alias=True)
    prompt = body.pop("prompt", None)
    if prompt is not None:
        body["task"] = prompt
    elif all(getattr(config, name) is None for name in _RUNNER_FIELDS):
        body["task"] = default_inbound_prompt(org, org_name)
    body["phone_number"] = caller
    body["from"] = called
    try:
        request = SendCallRequest.model_validate(body)
    except ValidationError as exc:
        error = exc.errors()[0]
        raise CallRequestError(
            "INVALID_INPUT", str(error["msg"]), field_path(tuple(error["loc"])) or None
        ) from exc
    request.check_rules()
    return request
