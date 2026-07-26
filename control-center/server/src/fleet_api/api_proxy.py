from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse

from fleet_api.auth.auth import SESSION_COOKIE_NAME, LocalUserRegistry, LoginRequest, registry_path_from_env
from fleet_api.models import HealthResponse
from fleet_api.release_metadata import service_release_id

_HOP_BY_HOP_HEADERS = {"connection", "content-length", "host", "keep-alive", "transfer-encoding"}

# A bound-strategy start performs read-only exchange boundary verification before
# the executor acknowledges the command.  HTTPX defaults to a five-second read
# timeout, which is shorter than that legitimate preflight.  Keep the timeout
# local to the Unix socket and never turn a timeout into a second command.
_EXECUTOR_CONNECT_TIMEOUT_SECONDS = 5.0
_EXECUTOR_COMMAND_ACK_TIMEOUT_SECONDS = 60.0
_EXECUTOR_READ_TIMEOUT_SECONDS = 15.0


def _executor_timeout() -> httpx.Timeout:
    return httpx.Timeout(
        connect=_EXECUTOR_CONNECT_TIMEOUT_SECONDS,
        read=_EXECUTOR_COMMAND_ACK_TIMEOUT_SECONDS,
        write=_EXECUTOR_READ_TIMEOUT_SECONDS,
        pool=_EXECUTOR_CONNECT_TIMEOUT_SECONDS,
    )


def _as_bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _forward_headers(request: Request) -> dict[str, str]:
    headers = {key: value for key, value in request.headers.items() if key.lower() not in _HOP_BY_HOP_HEADERS}
    # Browser supplied identity headers are never trusted. The proxy sets this
    # only after validating its HttpOnly local session cookie.
    headers.pop("x-fleet-user", None)
    owner = getattr(request.state, "fleet_user_id", None)
    if owner:
        headers["X-Fleet-User"] = owner
    return headers


def _response_headers(response: httpx.Response) -> dict[str, str]:
    return {key: value for key, value in response.headers.items() if key.lower() not in _HOP_BY_HOP_HEADERS}


def _opaque_executor_error(path: str, status_code: int) -> JSONResponse:
    if path.endswith("/strategy-run/prepare"):
        detail = (
            f"生成策略启动确认失败：执行器内部错误（HTTP {status_code}）。"
            "本次请求只进行只读预检，不会提交订单；请重试，若仍失败请检查执行器错误日志。"
        )
    else:
        detail = (
            f"执行器处理请求失败（HTTP {status_code}），但没有返回可读原因。"
            "系统没有自动重试该请求，请先核对当前状态后再操作。"
        )
    return JSONResponse(status_code=status_code, content={"detail": detail})


def _executor_socket() -> Path:
    return Path(os.environ.get("FLEET_EXECUTOR_SOCKET", "run/weex-fleet-executor.sock").strip()).expanduser()


def create_app(
    executor_socket: Path | None = None,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    api_release_id: str | None = None,
    user_registry: LocalUserRegistry | None = None,
    auth_required: bool | None = None,
) -> FastAPI:
    socket_path = executor_socket or _executor_socket()
    release_id = api_release_id or service_release_id("FLEET_API_RELEASE_ID")
    registry = user_registry or LocalUserRegistry(registry_path_from_env())
    require_auth = (
        _as_bool(os.environ.get("FLEET_LOCAL_USER_AUTH_REQUIRED", "true")) if auth_required is None else auth_required
    )
    cookie_secure = _as_bool(os.environ.get("FLEET_LOCAL_USER_COOKIE_SECURE", "false"))

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        selected_transport = transport or httpx.AsyncHTTPTransport(uds=str(socket_path))
        app.state.executor_client = httpx.AsyncClient(
            transport=selected_transport,
            base_url="http://fleet-executor",
            timeout=_executor_timeout(),
        )
        try:
            yield
        finally:
            await app.state.executor_client.aclose()

    app = FastAPI(title="WEEX Fleet Control API", version="0.1.0", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://127.0.0.1:37642", "http://localhost:37642"],
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "DELETE"],
        allow_headers=["Content-Type", "X-Fleet-Command-Id"],
    )

    @app.middleware("http")
    async def local_user_session(request: Request, call_next):
        public_paths = {
            "/api/v1/health",
            "/api/v1/auth/login",
            "/api/v1/auth/logout",
            "/api/v1/auth/me",
        }
        if not require_auth or request.url.path in public_paths:
            return await call_next(request)
        token = request.cookies.get(SESSION_COOKIE_NAME, "")
        try:
            user_id = registry.verify_session(token) if token else None
        except RuntimeError:
            user_id = None
        if user_id is None:
            return JSONResponse(status_code=status.HTTP_401_UNAUTHORIZED, content={"detail": "local login is required"})
        request.state.fleet_user_id = user_id
        return await call_next(request)

    @app.post("/api/v1/auth/login")
    async def login(payload: LoginRequest) -> Response:
        if not require_auth:
            return JSONResponse(
                status_code=status.HTTP_409_CONFLICT,
                content={"detail": "local user authentication is disabled"},
            )
        try:
            user = registry.authenticate(payload.username, payload.password.get_secret_value())
        except RuntimeError:
            user = None
        if user is None:
            return JSONResponse(
                status_code=status.HTTP_401_UNAUTHORIZED,
                content={"detail": "invalid local username or password"},
            )
        response = JSONResponse({"userId": user.user_id})
        response.set_cookie(
            SESSION_COOKIE_NAME,
            registry.issue_session(user),
            max_age=12 * 60 * 60,
            httponly=True,
            samesite="strict",
            secure=cookie_secure,
            path="/",
        )
        return response

    @app.get("/api/v1/auth/me")
    async def current_user(request: Request) -> Response:
        if not require_auth:
            return JSONResponse({"userId": "local"})
        token = request.cookies.get(SESSION_COOKIE_NAME, "")
        try:
            user_id = registry.verify_session(token) if token else None
        except RuntimeError:
            user_id = None
        if user_id is None:
            return JSONResponse(status_code=status.HTTP_401_UNAUTHORIZED, content={"detail": "local login is required"})
        return JSONResponse({"userId": user_id})

    @app.post("/api/v1/auth/logout")
    async def logout() -> Response:
        response = Response(status_code=status.HTTP_204_NO_CONTENT)
        response.delete_cookie(SESSION_COOKIE_NAME, path="/")
        return response

    @app.get("/api/v1/health", response_model=HealthResponse)
    async def health() -> HealthResponse:
        client: httpx.AsyncClient = app.state.executor_client
        try:
            response = await client.get("/_internal/executor-health")
            response.raise_for_status()
            executor = HealthResponse.model_validate(response.json())
        except httpx.HTTPError:
            return HealthResponse(
                status="degraded",
                adapter="unavailable",
                storage="unavailable",
                api_release_id=release_id,
                executor_connected=False,
            )
        return executor.model_copy(update={"api_release_id": release_id, "executor_connected": True})

    @app.get("/api/v1/events")
    async def events(request: Request) -> StreamingResponse:
        client: httpx.AsyncClient = app.state.executor_client

        async def stream() -> AsyncIterator[bytes]:
            try:
                async with client.stream(
                    "GET",
                    "/api/v1/events",
                    headers=_forward_headers(request),
                    params=request.query_params,
                    timeout=None,
                ) as response:
                    if response.status_code >= status.HTTP_400_BAD_REQUEST:
                        yield b'event: error\ndata: {"detail":"executor unavailable"}\n\n'
                        return
                    async for chunk in response.aiter_raw():
                        yield chunk
            except httpx.HTTPError:
                # EventSource reconnects by itself.  This deliberately reports
                # a stream interruption rather than claiming the executor or a
                # running strategy has failed.
                yield b'event: error\ndata: {"detail":"executor stream interrupted; reconnecting"}\n\n'

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
        )

    @app.get("/api/v1/instances/{instance_id}/strategy-monitor/events")
    async def strategy_monitor_events(instance_id: str, request: Request) -> StreamingResponse:
        client: httpx.AsyncClient = app.state.executor_client

        async def stream() -> AsyncIterator[bytes]:
            try:
                async with client.stream(
                    "GET",
                    f"/api/v1/instances/{instance_id}/strategy-monitor/events",
                    headers=_forward_headers(request),
                    params=request.query_params,
                    timeout=None,
                ) as response:
                    if response.status_code >= status.HTTP_400_BAD_REQUEST:
                        yield b'event: error\ndata: {"detail":"executor unavailable"}\n\n'
                        return
                    async for chunk in response.aiter_raw():
                        yield chunk
            except httpx.HTTPError:
                yield b'event: error\ndata: {"detail":"executor stream interrupted; reconnecting"}\n\n'

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
        )

    @app.api_route("/api/v1/{path:path}", methods=["GET", "POST", "PATCH", "DELETE"])
    async def forward(path: str, request: Request) -> Response:
        if request.method in {"POST", "PATCH", "DELETE"} and not request.headers.get("X-Fleet-Command-Id", "").strip():
            return JSONResponse(status_code=400, content={"detail": "X-Fleet-Command-Id is required"})
        client: httpx.AsyncClient = app.state.executor_client
        try:
            response = await client.request(
                request.method,
                f"/api/v1/{path}",
                params=request.query_params,
                content=await request.body(),
                headers=_forward_headers(request),
            )
        except httpx.ReadTimeout:
            if request.method in {"POST", "PATCH", "DELETE"}:
                return JSONResponse(
                    status_code=status.HTTP_504_GATEWAY_TIMEOUT,
                    content={
                        "detail": (
                            "执行器在 60 秒内没有确认该命令。系统没有自动重试，也不会重复提交订单；"
                            "请先查看当前任务状态，再决定是否重新操作。"
                        ),
                        "commandId": request.headers.get("X-Fleet-Command-Id", ""),
                    },
                )
            return JSONResponse(
                status_code=status.HTTP_504_GATEWAY_TIMEOUT,
                content={"detail": "读取执行器数据超时，请稍后重试；本次只读请求不会提交订单。"},
            )
        except httpx.ConnectError:
            return JSONResponse(
                status_code=503,
                content={"detail": "执行器当前无法连接。系统没有自动重试命令，也不会重复提交订单。"},
            )
        except httpx.HTTPError:
            return JSONResponse(
                status_code=502,
                content={"detail": "API 转发执行器请求失败。系统没有自动重试命令，也不会重复提交订单。"},
            )
        content_type = response.headers.get("content-type", "").lower()
        if response.status_code >= 500 and "application/json" not in content_type:
            return _opaque_executor_error(path, response.status_code)
        return Response(
            content=response.content,
            status_code=response.status_code,
            headers=_response_headers(response),
            media_type=response.headers.get("content-type"),
        )

    return app


app = create_app()
