"""Auth, cloud disclaimer, LLM provider, and Ollama control routes."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request, Response
from fastapi.responses import RedirectResponse

from app.deps import peer_host, rate_limit_ip, request_is_https, require_cloud_disclaimer_or_403
from app.schemas import (
    ApiKeyRequest,
    CloudDisclaimerRequest,
    LlmProviderRequest,
    OAuthCompleteRequest,
    OllamaEnableRequest,
    OllamaPullRequest,
    OllamaRestartRequest,
    SessionExchangeRequest,
)
from deepcatalog import config
from deepcatalog.auth import codex_auth_status
from deepcatalog.auth_rate_limit import (
    RATE_LIMIT_DETAIL,
    get_auth_rate_limiter,
    log_rate_limited,
    rate_limit_response_headers,
)
from deepcatalog.codex_oauth import (
    clear_auth,
    complete_oauth_login_manual,
    poll_oauth_login,
    save_api_key,
    start_oauth_login,
)
from deepcatalog.desktop_bootstrap import consume_desktop_bootstrap
from deepcatalog.inbox_worker import cancel_active_file, is_processing
from deepcatalog.job_control import get_active_file_id
from deepcatalog.llm import resolve_model_name
from deepcatalog.local_security import (
    COOKIE_NAME,
    auth_required_for_request,
    get_api_token,
    is_direct_loopback_request,
)
from deepcatalog.ollama_setup import (
    apply_llm_provider,
    enable_ollama,
    list_running_models,
    ollama_status,
    pull_model,
    restart_ollama,
    start_ollama,
    unload_model,
)
from deepcatalog.ollama_url import (
    is_loopback_ollama_url,
    normalize_ollama_base_url,
    public_llm_config_error,
    public_ollama_config_error,
)
from deepcatalog.privacy import (
    accept_cloud_disclaimer,
    cloud_disclaimer_status,
    revoke_cloud_disclaimer,
)
from deepcatalog.sessions import (
    api_token_accepted,
    attach_session_cookie,
    create_session,
    revoke_session,
    session_is_valid,
    session_ttl_seconds,
)
from deepcatalog.usage import usage_snapshot
from deepcatalog.version import get_current_version

logger = logging.getLogger(__name__)
router = APIRouter(tags=["auth"])


@router.get("/api/auth/session/status")
def api_session_status(request: Request) -> dict[str, Any]:
    """Whether API auth is required and whether this browser already has a session."""
    required = auth_required_for_request(
        peer_host=peer_host(request),
        host_header=request.headers.get("host"),
    )
    cookie = request.cookies.get(COOKIE_NAME)
    if not required:
        authenticated = True
    elif not get_api_token():
        authenticated = False
    else:
        authenticated = session_is_valid(cookie)
    return {
        "status": "success",
        "auth_required": required,
        "authenticated": authenticated,
        "session_ttl_seconds": session_ttl_seconds(),
    }


@router.get("/api/auth/desktop-bootstrap/{nonce}")
def api_desktop_bootstrap(nonce: str, request: Request) -> RedirectResponse:
    """Consume a one-time desktop nonce and set the HttpOnly session cookie."""
    if not is_direct_loopback_request(
        peer_host=peer_host(request),
        host_header=request.headers.get("host"),
    ):
        raise HTTPException(status_code=403, detail="desktop bootstrap is loopback-only")
    limiter = get_auth_rate_limiter()
    client_ip = rate_limit_ip(request)
    allowed, retry_after = limiter.check_session(client_ip)
    if not allowed:
        log_rate_limited(client_ip, request.url.path, retry_after)
        raise HTTPException(
            status_code=429,
            detail=RATE_LIMIT_DETAIL,
            headers=rate_limit_response_headers(retry_after),
        )
    if not consume_desktop_bootstrap(nonce):
        limiter.record_session_failure(client_ip)
        raise HTTPException(status_code=401, detail="invalid or expired desktop bootstrap")
    limiter.record_session_success(client_ip)
    raw = create_session()
    target = "/?desktop=1" if request.query_params.get("desktop") == "1" else "/"
    redirect = RedirectResponse(url=target, status_code=303)
    attach_session_cookie(redirect, raw, secure=request_is_https(request))
    return redirect


@router.post("/api/auth/session")
def api_session_exchange(
    request: Request, body: SessionExchangeRequest, response: Response
) -> dict[str, Any]:
    """Exchange DEEPCATALOG_API_TOKEN for an HttpOnly session cookie (secret never stored in JS)."""
    if not get_api_token():
        raise HTTPException(status_code=400, detail="DEEPCATALOG_API_TOKEN is not configured")
    limiter = get_auth_rate_limiter()
    client_ip = rate_limit_ip(request)
    allowed, retry_after = limiter.check_session(client_ip)
    if not allowed:
        log_rate_limited(client_ip, request.url.path, retry_after)
        raise HTTPException(
            status_code=429,
            detail=RATE_LIMIT_DETAIL,
            headers=rate_limit_response_headers(retry_after),
        )
    if not api_token_accepted(body.token):
        limiter.record_session_failure(client_ip)
        raise HTTPException(status_code=401, detail="invalid API token")
    limiter.record_session_success(client_ip)
    raw = create_session()
    attach_session_cookie(response, raw, secure=request_is_https(request))
    return {
        "status": "success",
        "authenticated": True,
        "session_ttl_seconds": session_ttl_seconds(),
    }


@router.post("/api/auth/session/logout")
def api_session_logout(request: Request, response: Response) -> dict[str, Any]:
    """Revoke the current browser session and clear the cookie."""
    raw = request.cookies.get(COOKIE_NAME)
    revoked = revoke_session(raw)
    response.delete_cookie(key=COOKIE_NAME, path="/")
    return {"status": "success", "revoked": revoked}


@router.get("/api/health")
def health() -> dict[str, Any]:
    """Unauthenticated liveness probe — no models, auth, usage, or paths."""
    return {"status": "ok", "version": get_current_version()}


@router.get("/api/diagnostics")
def diagnostics() -> dict[str, Any]:
    """Authenticated status for the UI: models, usage, auth, Ollama, disclaimer."""
    payload: dict[str, Any] = {
        "status": "ok",
        "version": get_current_version(),
        "llm_provider": config.LLM_PROVIDER,
        "model": resolve_model_name(),
        "configured_model": config.MODEL_NAME,
        "embedding_model": config.EMBEDDING_MODEL,
        "auth": codex_auth_status(),
        "cloud_disclaimer": cloud_disclaimer_status(),
        "usage": usage_snapshot(),
    }
    if config.LLM_PROVIDER == "ollama":
        ollama = ollama_status()
        payload["ollama"] = ollama
        if not ollama.get("ready"):
            payload["status"] = "degraded"
            payload["llm_error"] = ollama.get("error") or (
                f"Missing Ollama models: {', '.join(ollama.get('missing_models') or [])}"
                if ollama.get("missing_models")
                else "Local Ollama is not ready"
            )
    return payload


@router.get("/api/auth/status")
def api_auth_status() -> dict[str, Any]:
    return {
        "status": "success",
        **codex_auth_status(),
        "cloud_disclaimer": cloud_disclaimer_status(),
    }


@router.get("/api/privacy/cloud-disclaimer")
def api_cloud_disclaimer_status() -> dict[str, Any]:
    return {"status": "success", "cloud_disclaimer": cloud_disclaimer_status()}


@router.post("/api/privacy/cloud-disclaimer")
def api_cloud_disclaimer_set(body: CloudDisclaimerRequest) -> dict[str, Any]:
    status = accept_cloud_disclaimer() if body.accepted else revoke_cloud_disclaimer()
    return {"status": "success", "cloud_disclaimer": status}


@router.post("/api/auth/openai/start")
def api_auth_openai_start() -> dict[str, Any]:
    require_cloud_disclaimer_or_403()
    try:
        return start_oauth_login()
    except Exception:
        logger.exception("could not start OpenAI sign-in")
        raise HTTPException(status_code=500, detail="Could not start sign-in") from None


@router.get("/api/auth/openai/poll")
def api_auth_openai_poll(state: str = Query(..., min_length=8)) -> dict[str, Any]:
    return poll_oauth_login(state)


@router.post("/api/auth/openai/complete")
def api_auth_openai_complete(body: OAuthCompleteRequest) -> dict[str, Any]:
    require_cloud_disclaimer_or_403()
    try:
        return complete_oauth_login_manual(state=body.state, raw=body.callback)
    except Exception:
        logger.exception("could not complete OpenAI sign-in")
        raise HTTPException(status_code=400, detail="Could not complete sign-in") from None


@router.post("/api/auth/api-key")
def api_auth_api_key(body: ApiKeyRequest) -> dict[str, Any]:
    require_cloud_disclaimer_or_403()
    try:
        return save_api_key(body.api_key)
    except Exception:
        logger.exception("could not save API key")
        raise HTTPException(status_code=400, detail="Could not save API key") from None


@router.post("/api/auth/logout")
def api_auth_logout() -> dict[str, Any]:
    return clear_auth()


@router.get("/api/ollama/status")
def api_ollama_status() -> dict[str, Any]:
    return {"status": "success", "ollama": ollama_status()}


@router.post("/api/ollama/enable")
def api_ollama_enable(body: OllamaEnableRequest) -> dict[str, Any]:
    candidate = normalize_ollama_base_url(body.base_url) if body.base_url else None
    remote = bool(body.allow_remote) or (
        candidate is not None and not is_loopback_ollama_url(candidate)
    )
    if remote:
        if not body.allow_remote:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Non-loopback Ollama URLs require allow_remote=true and the privacy "
                    "disclaimer (documents leave this machine)."
                ),
            )
        require_cloud_disclaimer_or_403()
    try:
        result = enable_ollama(
            base_url=body.base_url,
            chat_model=body.chat_model,
            embedding_model=body.embedding_model,
            allow_remote=body.allow_remote,
            persist=True,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=public_ollama_config_error(exc)) from None

    pulled: list[dict[str, Any]] = []
    if body.pull_missing:
        for name in list(result.get("ollama", {}).get("missing_models") or []):
            try:
                pulled.append(pull_model(name, base_url=result["applied"]["ollama_base_url"]))
            except Exception:
                logger.exception("ollama pull failed")
                raise HTTPException(status_code=502, detail="Could not pull Ollama model") from None
        result["ollama"] = ollama_status(base_url=result["applied"]["ollama_base_url"])
        result["pulled"] = pulled
    return result


@router.post("/api/ollama/start")
def api_ollama_start() -> dict[str, Any]:
    try:
        return start_ollama()
    except RuntimeError:
        logger.exception("could not start Ollama")
        raise HTTPException(status_code=502, detail="Could not start Ollama") from None


@router.post("/api/ollama/pull")
def api_ollama_pull(body: OllamaPullRequest) -> dict[str, Any]:
    try:
        return pull_model(body.model)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid Ollama model name") from None
    except RuntimeError:
        logger.exception("ollama pull failed")
        raise HTTPException(status_code=502, detail="Could not pull Ollama model") from None


@router.post("/api/llm/provider")
def api_llm_provider(body: LlmProviderRequest) -> dict[str, Any]:
    provider = (body.provider or "").strip().lower()
    if provider in {"openai", "codex", "gemini", "google"}:
        require_cloud_disclaimer_or_403()
    candidate = normalize_ollama_base_url(body.base_url) if body.base_url else None
    remote_ollama = provider in {"ollama", "local"} and (
        bool(body.allow_remote) or (candidate is not None and not is_loopback_ollama_url(candidate))
    )
    if remote_ollama:
        if not body.allow_remote:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Non-loopback Ollama URLs require allow_remote=true and the privacy "
                    "disclaimer (documents leave this machine)."
                ),
            )
        require_cloud_disclaimer_or_403()
    try:
        applied = apply_llm_provider(
            body.provider,
            model=body.model,
            embedding_model=body.embedding_model,
            base_url=body.base_url,
            allow_remote=body.allow_remote,
            persist=True,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=public_llm_config_error(exc)) from None
    payload: dict[str, Any] = {"status": "success", "applied": applied}
    if applied["provider"] == "ollama":
        payload["ollama"] = ollama_status(base_url=applied["ollama_base_url"])
    return payload


@router.get("/api/ollama/ps")
def api_ollama_ps() -> dict[str, Any]:
    try:
        return list_running_models()
    except RuntimeError:
        logger.exception("ollama ps failed")
        raise HTTPException(status_code=503, detail="Ollama is not reachable") from None


@router.post("/api/ollama/unload")
def api_ollama_unload() -> dict[str, Any]:
    try:
        return unload_model()
    except RuntimeError:
        logger.exception("ollama unload failed")
        raise HTTPException(status_code=503, detail="Ollama is not reachable") from None


@router.post("/api/ollama/restart")
async def api_ollama_restart(body: OllamaRestartRequest) -> dict[str, Any]:
    if is_processing() and not body.force:
        raise HTTPException(
            status_code=409,
            detail="documents are being processed — cancel the active file or use force=true",
        )
    if is_processing() and body.force:
        file_id = get_active_file_id()
        if file_id:
            await cancel_active_file(file_id)
        for _ in range(30):
            if not is_processing():
                break
            await asyncio.sleep(0.1)
        if is_processing():
            raise HTTPException(
                status_code=409,
                detail="could not stop processing — try again in a moment",
            )
    try:
        return restart_ollama()
    except RuntimeError:
        logger.exception("ollama restart failed")
        raise HTTPException(status_code=503, detail="Could not restart Ollama") from None
