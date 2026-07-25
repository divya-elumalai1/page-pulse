import logging
import os
import uuid

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from app.models import AuditRequest, AuditResult, ErrorResponse
from app.cache import TTLCache
from app.rate_limiter import RateLimiter
from app.audit import run_audit, AuditError

# ---- Configuration (all overridable via env vars) --------------------------
CACHE_TTL_SECONDS = int(os.getenv("CACHE_TTL_SECONDS", "300"))
RATE_LIMIT_PER_MINUTE = int(os.getenv("RATE_LIMIT_PER_MINUTE", "30"))
REQUEST_TIMEOUT_SECONDS = float(os.getenv("REQUEST_TIMEOUT_SECONDS", "8"))
MAX_CONCURRENT_AUDITS = int(os.getenv("MAX_CONCURRENT_AUDITS", "20"))

# ---- Structured logging -----------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format='{"time":"%(asctime)s","level":"%(levelname)s","msg":"%(message)s"}',
)
logger = logging.getLogger("page-pulse")

# ---- App state ---------------------------------------------------------------
app = FastAPI(
    title="Page Pulse - URL Audit Service",
    version="1.0.0",
    description="Production-grade URL audit service. Built for Digital Heroes Training Task.",
)

cache = TTLCache(ttl_seconds=CACHE_TTL_SECONDS)
rate_limiter = RateLimiter(requests_per_minute=RATE_LIMIT_PER_MINUTE)

import asyncio
concurrency_semaphore = asyncio.Semaphore(MAX_CONCURRENT_AUDITS)


def get_client_id(request: Request) -> str:
    # Behind a real load balancer you'd read X-Forwarded-For; fall back to
    # the direct client host for local/dev use.
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


@app.middleware("http")
async def request_id_and_logging(request: Request, call_next):
    request_id = str(uuid.uuid4())
    request.state.request_id = request_id
    logger.info(f'request_id={request_id} path="{request.url.path}" method={request.method} - start')
    response = await call_next(request)
    logger.info(
        f'request_id={request_id} path="{request.url.path}" '
        f"status={response.status_code} - done"
    )
    response.headers["X-Request-ID"] = request_id
    return response


@app.get("/health")
async def health():
    return {"status": "ok", "cache_entries": len(cache)}


@app.post("/audit", response_model=AuditResult, responses={429: {"model": ErrorResponse}, 400: {"model": ErrorResponse}, 502: {"model": ErrorResponse}})
async def audit_url(payload: AuditRequest, request: Request):
    request_id = request.state.request_id
    client_id = get_client_id(request)
    url = str(payload.url)

    # --- Rate limiting ---
    if not rate_limiter.allow(client_id):
        logger.info(f"request_id={request_id} client={client_id} - rate limited")
        raise HTTPException(
            status_code=429,
            detail={
                "error": "RATE_LIMITED",
                "detail": f"Rate limit of {RATE_LIMIT_PER_MINUTE} requests/minute exceeded",
                "request_id": request_id,
            },
        )

    # --- Cache check ---
    cached = cache.get(url)
    if cached is not None:
        logger.info(f"request_id={request_id} client={client_id} url={url} - cache hit")
        result = dict(cached)
        result["from_cache"] = True
        return AuditResult(**result)

    # --- Concurrency limiting ---
    if concurrency_semaphore.locked() and concurrency_semaphore._value == 0:
        logger.info(f"request_id={request_id} client={client_id} - concurrency limit hit")
        raise HTTPException(
            status_code=503,
            detail={
                "error": "SERVICE_BUSY",
                "detail": "Too many concurrent audits in progress, try again shortly",
                "request_id": request_id,
            },
        )

    async with concurrency_semaphore:
        try:
            result = await run_audit(url, timeout_seconds=REQUEST_TIMEOUT_SECONDS)
        except AuditError as e:
            logger.info(f"request_id={request_id} client={client_id} url={url} - audit error {e.code}")
            raise HTTPException(
                status_code=502,
                detail={"error": e.code, "detail": e.message, "request_id": request_id},
            )

    cache.set(url, result)
    logger.info(f"request_id={request_id} client={client_id} url={url} - audit complete")
    return AuditResult(**result)


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    # Normalize FastAPI's default HTTPException shape to our structured
    # error format when `detail` is already a dict (from raises above).
    if isinstance(exc.detail, dict):
        return JSONResponse(status_code=exc.status_code, content=exc.detail)
    request_id = getattr(request.state, "request_id", "unknown")
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": "ERROR", "detail": str(exc.detail), "request_id": request_id},
    )


@app.exception_handler(ValidationError)
async def validation_exception_handler(request: Request, exc: ValidationError):
    request_id = getattr(request.state, "request_id", "unknown")
    return JSONResponse(
        status_code=400,
        content={"error": "INVALID_INPUT", "detail": str(exc), "request_id": request_id},
    )
