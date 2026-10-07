from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.types import PaginatedRequestParams

from .contracts import Contracts

TRANSIENT_ERRORS = (httpx2.TimeoutException, httpx2.TransportError, asyncio.TimeoutError)


class ToolCallError(RuntimeError):
    """The MCP server answered with an error result (not found, bad arguments, ...)."""


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    params: tuple[str, ...]
    required: tuple[str, ...]
    enums: dict[str, tuple[str, ...]]


def _field(obj: Any, *names: str) -> Any:
    # mcp>=2 exposes snake_case attributes; older clients used camelCase.
    for name in names:
        value = getattr(obj, name, None)
        if value is not None:
            return value
    return None


def _tool_spec(tool: Any) -> ToolSpec:
    schema = _field(tool, "input_schema", "inputSchema") or {}
    properties = schema.get("properties") or {}
    enums = {
        name: tuple(str(item) for item in prop["enum"])
        for name, prop in properties.items()
        if isinstance(prop, dict) and isinstance(prop.get("enum"), list)
    }
    return ToolSpec(
        name=tool.name,
        description=_field(tool, "description") or "",
        params=tuple(properties),
        required=tuple(schema.get("required") or ()),
        enums=enums,
    )


class EvidenceGateway:
    def __init__(self, session: ClientSession, contracts: Contracts, max_attempts: int = 2) -> None:
        self._session = session
        self._contracts = contracts
        self._max_attempts = max_attempts
        self._tools: list[ToolSpec] | None = None

    async def describe_tools(self) -> list[ToolSpec]:
        if self._tools is None:
            specs: list[ToolSpec] = []
            cursor: str | None = None
            while True:
                params = PaginatedRequestParams(cursor=cursor) if cursor else None
                response = await self._session.list_tools(params=params)
                specs.extend(_tool_spec(tool) for tool in response.tools)
                cursor = _field(response, "next_cursor", "nextCursor")
                if not cursor:
                    break
            self._tools = sorted(specs, key=lambda spec: spec.name)
        return self._tools

    async def list_tools(self) -> list[str]:
        return [spec.name for spec in await self.describe_tools()]

    async def call(self, tool_name: str, *, case_id: str, **arguments: Any) -> dict[str, Any]:
        payload = {"case_id": case_id, **arguments}
        for attempt in range(1, self._max_attempts + 1):
            try:
                result = await self._session.call_tool(tool_name, arguments=payload)
                break
            except TRANSIENT_ERRORS:
                # Evidence reads are idempotent, so one bounded retry is safe.
                if attempt == self._max_attempts:
                    raise
                await asyncio.sleep(0.5 * attempt)
        if _field(result, "is_error", "isError"):
            message = " ".join(
                block.text for block in result.content if getattr(block, "text", None)
            )
            raise ToolCallError(f"MCP tool {tool_name} failed: {message or 'unknown error'}")
        evidence = _field(result, "structured_content", "structuredContent")
        if evidence is None:
            text_blocks = [block.text for block in result.content if getattr(block, "text", None)]
            if len(text_blocks) != 1:
                raise ValueError(f"MCP tool {tool_name} did not return one evidence object")
            evidence = json.loads(text_blocks[0])
        self._contracts.validate_evidence(evidence, f"MCP tool {tool_name}")
        return evidence


@asynccontextmanager
async def connect_gateway(
    endpoint: str, team_api_key: str, contracts: Contracts
) -> AsyncIterator[EvidenceGateway]:
    headers = {"Authorization": f"Bearer {team_api_key}"}
    timeout = httpx2.Timeout(300.0, connect=30.0, write=30.0, pool=30.0)
    async with (
        httpx2.AsyncClient(headers=headers, timeout=timeout) as http_client,
        streamable_http_client(endpoint, http_client=http_client) as (read_stream, write_stream),
        ClientSession(read_stream, write_stream) as session,
    ):
        await session.initialize()
        yield EvidenceGateway(session, contracts)
