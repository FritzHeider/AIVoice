"""Fakes of the call container pieces: a recording ``CallWorkflow`` starter, a recording canceller, a DNS
resolver, an S3 client that presigns recording URLs and answers ``head_object``, and a scripted runner of
synchronous regression tests."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final, Literal, cast
from uuid import UUID

from botocore.exceptions import ClientError
from dns_fakes import MappedResolver

from va_api.services.call_starter import CallStartError
from va_api.services.call_stop import CallCancelError
from va_api.services.pathway_test_runs import RunStartError
from va_common.storage import ObjectStore
from va_domain.call_workflow import CallWorkflowInput
from va_domain.pathway_tests import PathwayTestWorkflowInput
from va_pathways.recording import Item
from va_pathways.runs import StoredTest

if TYPE_CHECKING:
    from types_aiobotocore_s3 import S3Client

__all__ = [
    "PRESIGN_HOST",
    "PUBLIC_ADDRESS",
    "FakeCallCanceller",
    "FakeCallStarter",
    "FakePathwayTestRunner",
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
class FakePathwayTestStarter:
    """Records every run start; raises ``fail`` (when set) instead of starting."""

    started: list[PathwayTestWorkflowInput] = field(default_factory=list)
    fail: RunStartError | None = None

    async def start(self, run: PathwayTestWorkflowInput) -> None:
        if self.fail is not None:
            raise self.fail
        self.started.append(run)


type Script = Literal["passed", "failed", "error", "raise", "hang"]


@dataclass
class FakePathwayTestRunner:
    """Answers each test by its script (``script[test_id]``, default ``passed``): an item of that status,
    ``raise`` (a ``RuntimeError``), or ``hang`` (never returns). ``before`` runs once before the first test;
    while ``gate`` is set to an unset event every test waits for it. Records the tests it ran and the most
    tests in flight at once; ``started`` is set once a test began, ``saturated`` once ``saturate_at`` tests
    are in flight together."""

    script: dict[str, Script] = field(default_factory=dict)
    before: Callable[[], Awaitable[None]] | None = None
    gate: asyncio.Event | None = None
    ran: list[str] = field(default_factory=list)
    in_flight: int = 0
    max_in_flight: int = 0
    started: asyncio.Event = field(default_factory=asyncio.Event)
    saturate_at: int | None = None
    saturated: asyncio.Event = field(default_factory=asyncio.Event)

    async def run(self, test: StoredTest) -> Item:
        if self.before is not None:
            before, self.before = self.before, None
            await before()
        test_id = str(test.test_id)
        self.ran.append(test_id)
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        self.started.set()
        if self.saturate_at is not None and self.in_flight >= self.saturate_at:
            self.saturated.set()
        try:
            if self.gate is not None:
                await self.gate.wait()
            outcome = self.script.get(test_id, "passed")
            if outcome == "hang":
                await asyncio.Event().wait()
            if outcome == "raise":
                msg = "runner exploded"
                raise RuntimeError(msg)
        finally:
            self.in_flight -= 1
        return {
            "test_id": test_id,
            "name": None,
            "passed": outcome == "passed",
            "status": outcome,
            "cases": [],
            "duration_ms": 1,
        }


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
class FakeResolver(MappedResolver):
    """Answers ``answers[host]`` (default: one public address); records every lookup."""

    default: list[str] | None = field(default_factory=lambda: [PUBLIC_ADDRESS])


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
