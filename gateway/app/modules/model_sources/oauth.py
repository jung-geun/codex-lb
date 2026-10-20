from __future__ import annotations

# PKCE URLs and code exchange adapted from KarpelesLab/teamclaude v1.1.25
# (88fe9b37862c40fefa459a90cd3dbd2c31db98ce), MIT, copyright (c) 2026
# KarpelesLab. Full permission notice: THIRD_PARTY_LICENSES.md.
import base64
import hashlib
import hmac
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from urllib.parse import parse_qs, urlencode, urlsplit

from fastapi import Request, Response

from app.core.auth.dashboard_access import DashboardPrincipal
from app.modules.dashboard_auth.service import DASHBOARD_SESSION_COOKIE, get_dashboard_session_store
from app.modules.model_sources.provider_schemas import OAuthStartResponse, OAuthStatusResponse
from app.modules.model_sources.providers import claude
from app.modules.model_sources.providers.base import ProviderError, utcnow

FLOW_COOKIE = "codex_lb_subscription_flow_session"
FLOW_TTL = timedelta(minutes=10)
MAX_FLOWS = 512


@dataclass(slots=True)
class OAuthFlow:
    flow_id: str
    source_id: str
    binding: str
    state: str = field(repr=False)
    verifier: str = field(repr=False)
    expires_at: datetime
    credential_generation: int | None
    source_configuration: str
    status: str = "pending"
    error: str | None = None


class OAuthFlows:
    """Bounded worker-local attempts; restart fails closed, never resumes tokens.

    State transitions happen without awaits. Claiming an attempt therefore
    makes complete/cancel races atomic inside the owning worker. OAuth flow
    requests must reach that worker (as existing dashboard login sessions do).
    """

    def __init__(self) -> None:
        self._flows: dict[str, OAuthFlow] = {}

    def start(self, source_id: str, binding: str, *, generation: int | None, configuration: str) -> OAuthStartResponse:
        now = utcnow()
        self._flows = {key: flow for key, flow in self._flows.items() if flow.expires_at + FLOW_TTL > now}
        if len(self._flows) >= MAX_FLOWS:
            raise ProviderError("Too many OAuth attempts; wait for existing attempts to expire")
        # Starting a new attempt invalidates older links in this same session.
        for flow in self._flows.values():
            if flow.source_id == source_id and flow.binding == binding and flow.status == "pending":
                flow.status = "cancelled"
                flow.verifier = ""
        flow = OAuthFlow(
            secrets.token_urlsafe(24),
            source_id,
            binding,
            secrets.token_urlsafe(32),
            secrets.token_urlsafe(48),
            now + FLOW_TTL,
            generation,
            configuration,
        )
        self._flows[flow.flow_id] = flow
        challenge = base64.urlsafe_b64encode(hashlib.sha256(flow.verifier.encode()).digest()).rstrip(b"=").decode()
        url = (
            claude.AUTHORIZE_URL
            + "?"
            + urlencode(
                {
                    "code": "true",
                    "client_id": claude.CLIENT_ID,
                    "response_type": "code",
                    "redirect_uri": claude.REDIRECT_URI,
                    "scope": claude.SCOPES,
                    "code_challenge": challenge,
                    "code_challenge_method": "S256",
                    "state": flow.state,
                }
            )
        )
        return OAuthStartResponse(flow_id=flow.flow_id, authorization_url=url, expires_at=flow.expires_at)

    def get(self, source_id: str, flow_id: str, binding: str) -> OAuthFlow:
        flow = self._flows.get(flow_id)
        if flow is None or flow.source_id != source_id or not hmac.compare_digest(flow.binding, binding):
            raise ProviderError("OAuth attempt not found for this dashboard session")
        if flow.expires_at <= utcnow() and flow.status in {"pending", "exchanging"}:
            flow.status = "expired"
            flow.verifier = ""
        return flow

    def status(self, source_id: str, flow_id: str, binding: str) -> OAuthStatusResponse:
        flow = self.get(source_id, flow_id, binding)
        return OAuthStatusResponse(
            flow_id=flow.flow_id, status=flow.status, expires_at=flow.expires_at, error=flow.error
        )

    def cancel(self, source_id: str, flow_id: str, binding: str) -> None:
        flow = self.get(source_id, flow_id, binding)
        if flow.status == "exchanging":
            raise ProviderError("OAuth code was already submitted and cannot be cancelled")
        if flow.status == "pending":
            flow.status = "cancelled"
            flow.verifier = ""

    async def exchange(
        self, source_id: str, flow_id: str, binding: str, pasted: str
    ) -> tuple[OAuthFlow, claude.TokenPair]:
        flow = self.get(source_id, flow_id, binding)
        if flow.status != "pending":
            raise ProviderError("OAuth attempt has already been used, cancelled, or expired")
        # Consume even malformed attempts; neither state nor PKCE can be reused.
        flow.status = "exchanging"
        verifier = flow.verifier
        flow.verifier = ""
        try:
            code = parse_code(pasted, flow.state)
            pair = await claude.token_request(
                {
                    "code": code,
                    "state": flow.state,
                    "grant_type": "authorization_code",
                    "client_id": claude.CLIENT_ID,
                    "redirect_uri": claude.REDIRECT_URI,
                    "code_verifier": verifier,
                }
            )
            if self.get(source_id, flow_id, binding).status != "exchanging":
                raise ProviderError("OAuth attempt was cancelled or expired")
            return flow, pair
        except BaseException:
            if flow.status == "exchanging":
                flow.status = "failed"
                flow.error = "Authorization could not be completed; start a new sign-in"
            raise


def parse_code(pasted: str, state: str) -> str:
    text = pasted.strip()
    supplied_state = None
    if "://" in text:
        url = urlsplit(text)
        if f"{url.scheme}://{url.netloc}{url.path}" != claude.REDIRECT_URI:
            raise ProviderError("Paste the Claude callback URL or authorization code, not a sign-in link")
        query = parse_qs(url.query)
        supplied_state = query.get("state", [None])[0]
        code = query.get("code", [""])[0]
        if not supplied_state or "error" in query:
            raise ProviderError("Claude sign-in callback is missing state or was refused")
    elif "#" in text:
        code, supplied_state = text.split("#", 1)
    else:
        # A raw code remains bound to this attempt by PKCE and dashboard session.
        code = text
    if supplied_state is not None and not hmac.compare_digest(supplied_state, state):
        raise ProviderError("OAuth state mismatch; start a new sign-in")
    if not code or any(char.isspace() for char in code) or len(code) > 4096:
        raise ProviderError("Invalid Claude authorization code")
    return code


def session_binding(request: Request, principal: DashboardPrincipal, response: Response | None = None) -> str:
    session = request.cookies.get(DASHBOARD_SESSION_COOKIE)
    if session and get_dashboard_session_store().get(session) is None:
        raise ProviderError("A current dashboard session is required")
    if not session:
        # Header-auth/bootstrap modes have no dashboard login cookie. Mint a
        # browser-session identifier, never use principal/IP alone as session.
        session = request.cookies.get(FLOW_COOKIE)
        if not session:
            if response is None:
                raise ProviderError("Start OAuth in this dashboard browser session first")
            session = secrets.token_urlsafe(32)
            response.set_cookie(
                FLOW_COOKIE,
                session,
                httponly=True,
                secure=request.url.scheme == "https",
                samesite="strict",
                max_age=1200,
                path="/api/model-sources",
            )
    identity = (principal.user_id, principal.actor, principal.auth_method, principal.auth_mode.value)
    return hashlib.sha256((session + "\0" + repr(identity)).encode()).hexdigest()


flows = OAuthFlows()
