"""Fakes of the call container pieces: a recording ``CallWorkflow`` starter, a recording canceller, a DNS
resolver, and an S3 client that presigns recording URLs and answers ``head_object``."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final, cast
from uuid import UUID

from botocore.exceptions import ClientError

from va_api.services.call_starter import CallStartError
from va_api.services.call_stop import CallCancelError
from va_common.storage import ObjectStore
from va_domain.call_workflow import CallWorkflowInput

if TYPE_CHECKING:
    from types_aiobotocore_s3 import S3Client

__all__ = [
    "PRESIGN_HOST",
    "PUBLIC_ADDRESS",
    "FakeCallCanceller",
    "FakeCallStarter",
    "FakePresigner",
    "FakeResolver",
]

PUBLIC_ADDRESS: Final = "8.8.8.8"
PRESIGN_HOST: Final = "https://s3.test"


@dataclass
class FakeCallStarter:
    """Records every start; raises ``fail`` (when set) instead of starting."""

    started: list[CallWorkflowInput] = field(default_factory=list)
    fail: CallStartError | None = None

    async def start(self, call: CallWorkflowInput) -> None:
        if self.fail is not None:
            raise self.fail
        self.started.append(call)


@dataclass
class FakeCallCanceller:
    """Records every cancel; answers ``False`` (no workflow) when ``missing``, raises ``fail`` when set."""

    cancelled: list[UUID] = field(default_factory=list)
    missing: bool = False
    fail: CallCancelError | None = None

    async def cancel(self, call_id: UUID) -> bool:
        if self.fail is not None:
            raise self.fail
        self.cancelled.append(call_id)
        return not self.missing


@dataclass
class FakeResolver:
    """Answers ``answers[host]`` (default: one public address); records every lookup."""

    answers: dict[str, list[str]] = field(default_factory=dict)
    lookups: list[str] = field(default_factory=list)

    async def __call__(self, host: str) -> list[str]:
        self.lookups.append(host)
        return self.answers.get(host, [PUBLIC_ADDRESS])


@dataclass
class FakePresigner:
    """The presign and head calls of the S3 client: answers ``{PRESIGN_HOST}/{bucket}/{key}?expires=<s>`` and
    records each ``(bucket, key, expires)``; raises a ``ClientError`` while ``fail`` is set. ``head_object``
    finds the keys in ``existing`` (a 404 ``ClientError`` otherwise) and raises a 500 one while ``fail_head``
    is set."""

    presigned: list[tuple[str, str, int]] = field(default_factory=list)
    fail: bool = False
    existing: set[str] = field(default_factory=set)
    fail_head: bool = False
    heads: list[str] = field(default_factory=list)

    async def head_object(self, Bucket: str, Key: str) -> dict[str, Any]:  # noqa: N803 - boto3 names
        self.heads.append(Key)
        if self.fail_head:
            raise ClientError({"Error": {"Code": "500", "Message": "boom"}}, "HeadObject")
        if Key not in self.existing:
            raise ClientError({"Error": {"Code": "404", "Message": "Not Found"}}, "HeadObject")
        return {"Bucket": Bucket, "ContentLength": 1}

    async def generate_presigned_url(
        self,
        operation: str,
        Params: dict[str, Any],  # noqa: N803 - boto3 keyword names
        ExpiresIn: int,  # noqa: N803
    ) -> str:
        if self.fail:
            raise ClientError({"Error": {"Code": "AccessDenied", "Message": "denied"}}, operation)
        bucket, key = Params["Bucket"], Params["Key"]
        self.presigned.append((bucket, key, ExpiresIn))
        return f"{PRESIGN_HOST}/{bucket}/{key}?expires={ExpiresIn}"

    def store(self) -> ObjectStore:
        return ObjectStore(cast("S3Client", self))
