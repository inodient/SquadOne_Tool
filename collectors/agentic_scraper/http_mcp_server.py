"""FastMCP HTTP entrypoint for the Agentic Scrapper tool server.

Start:
    fastmcp run http_mcp_server.py --transport streamable-http --host 0.0.0.0 --port 8002

Or via uvicorn:
    uvicorn http_mcp_server:mcp.http_app --host 0.0.0.0 --port 8002
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path
from typing import Any

# fastmcp run puts only the file's parent on sys.path — add project root for core.* / react_agent.*
_PROJECT_ROOT = Path(__file__).resolve().parent
_root_str = str(_PROJECT_ROOT)
if _root_str not in sys.path:
    sys.path.insert(0, _root_str)

from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app.common.logging import get_logger
from app.server import discover_tools, handle_tool_call, health_check, readiness_check, settings


def _handle_tool_call_isolated(payload: dict[str, Any]) -> dict[str, Any]:
    """Wrap handle_tool_call in a clean thread context with no asyncio event loop.

    asyncio.to_thread threads can still see the parent event loop via
    asyncio.get_event_loop(), which causes langchain-google-genai 2.1.x to
    take an async retry code path that passes max_retries to the gRPC client
    (unsupported in google-ai-generativelanguage 0.11.x). Clearing the loop
    reference before the call forces the sync code path.
    """
    asyncio.set_event_loop(None)
    return handle_tool_call(payload)

mcp = FastMCP(settings.service_name)
REST_CONTRACT_BASE_PATH = "/v1"
MCP_REST_CONTRACT_BASE_PATH = "/mcp/v1"
logger = get_logger(__name__)


def _request_context(request: Request, payload: dict[str, Any] | None = None) -> dict[str, str]:
    trace_id = task_id = request_id = ""
    if payload is not None:
        trace_id   = str(payload.get("trace_id", "") or "")
        task_id    = str(payload.get("task_id", "") or "")
        request_id = str(payload.get("request_id", "") or "")
    client_ip = request.client.host if request.client else ""
    return {"trace_id": trace_id, "task_id": task_id, "request_id": request_id, "client_ip": client_ip}


def _log_http_request(request: Request, payload: dict[str, Any] | None = None) -> None:
    ctx = _request_context(request, payload)
    logger.info(
        "mcp_http_request",
        extra={**ctx, "method": request.method, "path": request.url.path},
    )


def _log_http_response(
    *,
    request: Request,
    started_at: float,
    status_code: int,
    payload: dict[str, Any] | None = None,
    result_body: dict[str, Any] | None = None,
) -> None:
    ctx = _request_context(request, payload)
    elapsed_ms = round((time.perf_counter() - started_at) * 1000, 2)
    result_status = error_code = ""
    if result_body:
        result_status = str(result_body.get("status", "") or "")
        err = result_body.get("error")
        if isinstance(err, dict):
            error_code = str(err.get("code", "") or "")
    logger.info(
        "mcp_http_response",
        extra={
            **ctx,
            "method": request.method,
            "path": request.url.path,
            "status_code": status_code,
            "elapsed_ms": elapsed_ms,
            "result_status": result_status,
            "error_code": error_code,
        },
    )


# ── Native MCP tools (for MCP protocol clients) ──────────────────────────────

@mcp.tool(name="discover_tools", description="Return tool manifest payload for SquadOne discover.")
def discover_tools_tool() -> dict[str, Any]:
    return discover_tools()


@mcp.tool(name="scrape", description="Invoke scrape with SquadOne envelope payload.")
def scrape_tool(payload: dict[str, Any]) -> dict[str, Any]:
    return handle_tool_call(payload)


@mcp.tool(name="health_check", description="Return liveness health checks.")
def health_check_tool() -> dict[str, Any]:
    return health_check()


@mcp.tool(name="readiness_check", description="Return readiness checks.")
def readiness_check_tool() -> dict[str, Any]:
    return readiness_check()


# ── REST shim routes (for SquadOne_AI HTTP transport) ────────────────────────

def _error_body(msg: str, protocol_version: str) -> dict[str, Any]:
    return {
        "status": "failed",
        "result": None,
        "evidence": [],
        "warnings": [],
        "error": {
            "code": "DATA_INPUT_INVALID",
            "message": msg,
            "retryable": False,
            "origin": "mcp_server",
        },
        "timing": {},
        "meta": {"protocol_version": protocol_version},
    }


@mcp.custom_route(path=f"{REST_CONTRACT_BASE_PATH}/tools", methods=["GET"])
async def rest_list_tools(request: Request) -> Response:
    started_at = time.perf_counter()
    _log_http_request(request)
    body = discover_tools()
    _log_http_response(request=request, started_at=started_at, status_code=200, result_body=body)
    return JSONResponse(body)


@mcp.custom_route(path=f"{MCP_REST_CONTRACT_BASE_PATH}/tools", methods=["GET"])
async def mcp_rest_list_tools(request: Request) -> Response:
    started_at = time.perf_counter()
    _log_http_request(request)
    body = discover_tools()
    _log_http_response(request=request, started_at=started_at, status_code=200, result_body=body)
    return JSONResponse(body)


@mcp.custom_route(path=f"{REST_CONTRACT_BASE_PATH}/invoke", methods=["POST"])
async def rest_invoke(request: Request) -> Response:
    started_at = time.perf_counter()
    payload = await request.json()
    typed_payload = payload if isinstance(payload, dict) else None
    _log_http_request(request, typed_payload)
    if not isinstance(payload, dict):
        body = _error_body("request body must be a JSON object", settings.protocol_version)
        _log_http_response(request=request, started_at=started_at, status_code=400, result_body=body)
        return JSONResponse(body, status_code=400)
    # Playwright sync API cannot run inside the asyncio event loop thread — offload to a worker thread
    body = await asyncio.to_thread(_handle_tool_call_isolated, payload)
    _log_http_response(request=request, started_at=started_at, status_code=200, payload=payload, result_body=body)
    return JSONResponse(body)


@mcp.custom_route(path=f"{MCP_REST_CONTRACT_BASE_PATH}/invoke", methods=["POST"])
async def mcp_rest_invoke(request: Request) -> Response:
    started_at = time.perf_counter()
    payload = await request.json()
    typed_payload = payload if isinstance(payload, dict) else None
    _log_http_request(request, typed_payload)
    if not isinstance(payload, dict):
        body = _error_body("request body must be a JSON object", settings.protocol_version)
        _log_http_response(request=request, started_at=started_at, status_code=400, result_body=body)
        return JSONResponse(body, status_code=400)
    body = await asyncio.to_thread(_handle_tool_call_isolated, payload)
    _log_http_response(request=request, started_at=started_at, status_code=200, payload=payload, result_body=body)
    return JSONResponse(body)
