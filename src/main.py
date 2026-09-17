from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any

import httpx
from fastapi import FastAPI, Header, HTTPException, Response
from prometheus_client import make_asgi_app
from pydantic import BaseModel, Field

from .config import get_settings
from .metrics import observe_record_failure, observe_route, observe_upstream_error
from .picking import POLICIES, normalize, pick

logger = logging.getLogger("router")

app = FastAPI(title="ai-infra-platform-router", version="0.1.0")
app.mount("/metrics", make_asgi_app())


class RouteIn(BaseModel):
    model_name: str = Field(min_length=1, max_length=200)
    prompt: str = Field(min_length=1)
    policy: str | None = None
    max_tokens: int | None = Field(default=None, gt=0, le=2048)
    engine_models: dict[str, str] = Field(default_factory=dict)


class BackendOut(BaseModel):
    id: int
    name: str
    engine: str
    url: str


class RouteOut(BaseModel):
    request_id: str | None
    recorded: bool
    backend: BackendOut
    decision: dict[str, Any]
    output: str
    usage: dict[str, Any]
    timings: dict[str, Any]


async def _fetch_candidates(
    client: httpx.AsyncClient, base: str, headers: dict[str, str], model_name: str, policy: str
) -> list[dict[str, Any]]:
    response = await client.get(
        f"{base}/api/v1/routing/candidates",
        params={"model_name": model_name, "policy": policy},
        headers=headers,
    )
    _raise_for_control_plane(response)
    return list(response.json().get("ranked") or [])


async def _fetch_backend(
    client: httpx.AsyncClient, base: str, headers: dict[str, str], backend_id: int
) -> dict[str, Any]:
    response = await client.get(
        f"{base}/api/v1/backends/{backend_id}", params={"with_models": False}, headers=headers
    )
    _raise_for_control_plane(response)
    return dict(response.json())


def _raise_for_control_plane(response: httpx.Response) -> None:
    if response.status_code == 401:
        raise HTTPException(
            status_code=401,
            detail={"code": "unauthorized", "message": "控制层拒绝了这把 key", "detail": {}},
        )
    if response.status_code >= 400:
        raise HTTPException(
            status_code=502,
            detail={
                "code": "control_plane_error",
                "message": "控制层返回错误",
                "detail": {"status": response.status_code, "body": response.text[:300]},
            },
        )


async def _call_backend(
    client: httpx.AsyncClient,
    engine: str,
    url: str,
    engine_model: str,
    prompt: str,
    max_tokens: int,
) -> tuple[str, int | None, int | None]:
    base = normalize(url)
    if engine == "ollama":
        response = await client.post(
            f"{base}/api/generate",
            json={
                "model": engine_model,
                "prompt": prompt,
                "stream": False,
                "options": {"num_predict": max_tokens},
            },
        )
        response.raise_for_status()
        data = response.json()
        return (
            str(data.get("response") or ""),
            data.get("prompt_eval_count"),
            data.get("eval_count"),
        )
    response = await client.post(
        f"{base}/v1/chat/completions",
        json={
            "model": engine_model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
            "temperature": 0,
        },
    )
    response.raise_for_status()
    data = response.json()
    choice = (data.get("choices") or [{}])[0]
    usage = data.get("usage") or {}
    return (
        str((choice.get("message") or {}).get("content") or ""),
        usage.get("prompt_tokens"),
        usage.get("completion_tokens"),
    )


async def _record(
    client: httpx.AsyncClient, base: str, headers: dict[str, str], payload: dict[str, Any]
) -> str | None:
    response = await client.post(f"{base}/api/v1/requests", json=payload, headers=headers)
    if response.status_code >= 400:
        logger.warning("写入请求日志失败: HTTP %s %s", response.status_code, response.text[:200])
        return None
    return str(response.json().get("request_id") or "")


@app.get("/healthz", summary="存活探针")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/readyz", summary="就绪探针")
async def readyz(response: Response) -> dict[str, Any]:
    settings = get_settings()
    base = normalize(settings.control_plane_url)
    failed: list[str] = []
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            probe = await client.get(f"{base}/healthz")
            if probe.status_code != 200:
                failed.append("control_plane")
    except Exception as exc:
        logger.warning("控制层探活失败: %r", exc)
        failed.append("control_plane")
    if failed:
        response.status_code = 503
    return {
        "status": "ok" if not failed else "degraded",
        "control_plane": base,
        "failed": failed,
    }


@app.post("/v1/route", response_model=RouteOut, summary="按控制层的决策选一个真实后端并执行一次生成")
async def route(
    payload: RouteIn,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> RouteOut:
    if not x_api_key:
        raise HTTPException(
            status_code=401,
            detail={"code": "unauthorized", "message": "缺少 X-API-Key 请求头", "detail": {}},
        )

    settings = get_settings()
    policy = payload.policy or settings.default_policy
    if policy not in POLICIES:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "invalid_policy",
                "message": "policy 取值非法",
                "detail": {"policy": policy, "allowed": sorted(POLICIES)},
            },
        )

    base = normalize(settings.control_plane_url)
    headers = {"X-API-Key": x_api_key}
    max_tokens = payload.max_tokens or settings.default_max_tokens
    started = datetime.now(timezone.utc)
    clock = time.perf_counter()

    async with httpx.AsyncClient(timeout=settings.request_timeout_seconds) as client:
        candidates = await _fetch_candidates(client, base, headers, payload.model_name, policy)
        chosen = pick(candidates, policy)
        if chosen is None:
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "no_candidate",
                    "message": "没有可用后端：候选为空，或全部被排除",
                    "detail": {"model_name": payload.model_name, "candidates": candidates},
                },
            )

        backend = await _fetch_backend(client, base, headers, int(chosen["backend_id"]))
        engine_model = payload.engine_models.get(str(backend["name"])) or payload.model_name

        output = ""
        prompt_tokens: int | None = None
        completion_tokens: int | None = None
        error_code: str | None = None
        try:
            output, prompt_tokens, completion_tokens = await _call_backend(
                client, str(backend["engine"]), str(backend["url"]), engine_model,
                payload.prompt, max_tokens,
            )
        except httpx.HTTPStatusError as exc:
            error_code = f"backend_http_{exc.response.status_code}"
            logger.warning("后端返回错误: %s %s", exc.response.status_code, exc.response.text[:200])
        except Exception as exc:
            error_code = type(exc).__name__
            logger.warning("后端调用失败: %r", exc)

        status = "success" if error_code is None else "error"
        total_ms = round((time.perf_counter() - clock) * 1000, 2)
        observe_route(str(backend["name"]), str(backend["engine"]), policy,
                      status, total_ms / 1000, len(candidates), payload.model_name)
        if error_code is not None:
            observe_upstream_error(error_code)
        decision = {
            "policy": policy,
            "candidate_ids": [int(item["backend_id"]) for item in candidates],
            "chosen_id": int(chosen["backend_id"]),
            "score_snapshot": {"ranked": candidates},
            "fallback_reason": None,
        }

        request_id: str | None = None
        recorded = False
        try:
            request_id = await _record(
                client,
                base,
                headers,
                {
                    "model_name": payload.model_name,
                    "status": status,
                    "started_at": started.isoformat(),
                    "finished_at": datetime.now(timezone.utc).isoformat(),
                    "backend_id": int(chosen["backend_id"]),
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "total_ms": total_ms,
                    "error_code": error_code,
                    "decision": decision,
                },
            )
            recorded = request_id is not None
        except Exception as exc:
            logger.warning("写入请求日志异常: %r", exc)
        if not recorded:
            observe_record_failure()

    if error_code is not None:
        raise HTTPException(
            status_code=502,
            detail={
                "code": error_code,
                "message": "后端生成失败（这次失败已经记进请求日志）",
                "detail": {"backend": backend["name"], "request_id": request_id, "recorded": recorded},
            },
        )

    return RouteOut(
        request_id=request_id,
        recorded=recorded,
        backend=BackendOut(
            id=int(backend["id"]),
            name=str(backend["name"]),
            engine=str(backend["engine"]),
            url=str(backend["url"]),
        ),
        decision=decision,
        output=output,
        usage={"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens},
        timings={"total_ms": total_ms, "engine_model": engine_model},
    )
