"""OAuth2 account-linking handlers for Alexa skill setup.

Flow
----
1.  User enables the skill in the Alexa app.
2.  Alexa redirects to  GET /auth/authorize?response_type=code&client_id=...&redirect_uri=...&state=...
3.  We render an HTML form asking for the user's Music Assistant URL and token.
4.  User submits the form (POST /auth/authorize).
    a. We test connectivity to their MA instance.
    b. On success we generate a short-lived auth code, persist the config,
       and redirect back to Alexa with  ?code=...&state=...
5.  Alexa exchanges the code:  POST /auth/token  (form-encoded, or HTTP Basic auth).
    We return a permanent access_token (UUID).
6.  Every subsequent Alexa request carries  context.System.user.accessToken = <UUID>.
    We use it to look up the user's MA config in SQLite.

OAuth credentials (ECHO_OAUTH_CLIENT_ID / ECHO_OAUTH_CLIENT_SECRET) are values
you define when setting up Account Linking in the Alexa developer console.
"""

from __future__ import annotations

import base64
import logging
from typing import Optional

import httpx
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from app.storage import db as storage

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])

# ── HTML form ────────────────────────────────────────────────────────────────

_FORM_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Echo Bridge — Link Your Music Assistant</title>
  <style>
    *,*::before,*::after{{box-sizing:border-box;margin:0;padding:0}}
    body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
         background:#0f172a;color:#e2e8f0;min-height:100vh;display:flex;
         align-items:center;justify-content:center;padding:1rem}}
    .card{{background:#1e293b;border:1px solid #334155;border-radius:14px;
           padding:2rem 2.25rem;width:100%;max-width:460px}}
    .logo{{display:flex;align-items:center;gap:.75rem;margin-bottom:1.75rem}}
    .logo-icon{{width:40px;height:40px;background:#1d4ed8;border-radius:10px;
                display:flex;align-items:center;justify-content:center;flex-shrink:0}}
    .logo-icon svg{{color:#93c5fd}}
    .logo-title{{font-size:1.125rem;font-weight:700;color:#f1f5f9;line-height:1.2}}
    .logo-sub{{font-size:.75rem;color:#64748b;margin-top:.1rem}}
    h2{{font-size:1rem;font-weight:600;color:#f1f5f9}}
    .sub{{font-size:.875rem;color:#94a3b8;margin:.35rem 0 1.5rem}}
    .field{{margin-bottom:1.1rem}}
    .field label{{display:block;font-size:.7rem;font-weight:700;color:#94a3b8;
                  text-transform:uppercase;letter-spacing:.06em;margin-bottom:.35rem}}
    .field input{{width:100%;padding:.625rem .875rem;background:#0f172a;
                  border:1.5px solid #334155;border-radius:8px;color:#f1f5f9;
                  font-size:.875rem;outline:none;transition:border-color .15s}}
    .field input:focus{{border-color:#3b82f6;box-shadow:0 0 0 3px rgba(59,130,246,.15)}}
    .field input::placeholder{{color:#475569}}
    .hint{{font-size:.72rem;color:#64748b;margin-top:.3rem;line-height:1.5}}
    .hint a{{color:#60a5fa;text-decoration:none}}
    .alert{{background:#450a0a;border:1px solid #7f1d1d;border-radius:8px;
            padding:.75rem 1rem;font-size:.85rem;color:#fca5a5;margin-bottom:1.25rem;
            display:flex;gap:.5rem;align-items:flex-start}}
    .alert svg{{flex-shrink:0;margin-top:.1rem}}
    .btn{{width:100%;padding:.8rem;background:#2563eb;color:#fff;font-size:.9375rem;
          font-weight:600;border:none;border-radius:8px;cursor:pointer;
          margin-top:.5rem;transition:background .15s;display:flex;
          align-items:center;justify-content:center;gap:.5rem}}
    .btn:hover{{background:#1d4ed8}}
    .btn:active{{background:#1e40af}}
    .divider{{border:none;border-top:1px solid #1e3a5f;margin:1.5rem 0}}
    .steps{{display:flex;flex-direction:column;gap:.7rem}}
    .step{{display:flex;gap:.75rem;align-items:flex-start}}
    .step-n{{width:1.375rem;height:1.375rem;border-radius:50%;background:#172554;
              color:#93c5fd;font-size:.7rem;font-weight:800;display:flex;
              align-items:center;justify-content:center;flex-shrink:0;margin-top:.15rem}}
    .step-t{{font-size:.78rem;color:#94a3b8;line-height:1.55}}
    .step-t strong{{color:#cbd5e1;font-weight:600}}
  </style>
</head>
<body>
<div class="card">
  <div class="logo">
    <div class="logo-icon">
      <svg width="22" height="22" viewBox="0 0 24 24" fill="none"
           stroke="currentColor" stroke-width="2.2" stroke-linecap="round">
        <path d="M9 18V5l12-2v13"/>
        <circle cx="6" cy="18" r="3"/><circle cx="18" cy="16" r="3"/>
      </svg>
    </div>
    <div>
      <div class="logo-title">Echo Bridge</div>
      <div class="logo-sub">Music Assistant &times; Alexa</div>
    </div>
  </div>

  {error_block}

  <h2>Link your Music Assistant</h2>
  <p class="sub">Enter your MA server details to enable voice control with Alexa.</p>

  <form method="POST" action="/auth/authorize">
    <input type="hidden" name="client_id"      value="{client_id}">
    <input type="hidden" name="redirect_uri"   value="{redirect_uri}">
    <input type="hidden" name="state"          value="{state}">
    <input type="hidden" name="response_type"  value="{response_type}">

    <div class="field">
      <label>Music Assistant URL</label>
      <input type="url" name="ma_url" required
             placeholder="http://192.168.1.42:8095" value="{ma_url}">
      <p class="hint">
        The URL of your MA server — visible in
        <strong>MA &rarr; Settings &rarr; Info</strong>.
        Can be a local IP, <code>homeassistant.local</code>, or a Nabu Casa URL.
      </p>
    </div>

    <div class="field">
      <label>Long-lived Access Token</label>
      <input type="password" name="ma_token" required
             placeholder="Paste your token here">
      <p class="hint">
        In Music Assistant go to
        <strong>Settings &rarr; User &rarr; Long-lived access tokens</strong>
        and click <strong>+&nbsp;Create token</strong>.
      </p>
    </div>

    <div class="field">
      <label>Default Player ID
        <span style="text-transform:none;font-weight:400;color:#475569">&nbsp;— optional</span>
      </label>
      <input type="text" name="player_id"
             placeholder="Leave blank to auto-select" value="{player_id}">
      <p class="hint">
        Only needed if you have multiple players and want to pin one.
        You can always re-link to change this.
      </p>
    </div>

    <button class="btn" type="submit">
      <svg width="18" height="18" viewBox="0 0 24 24" fill="none"
           stroke="currentColor" stroke-width="2.5" stroke-linecap="round">
        <path d="M15 3h4a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2h-4"/>
        <polyline points="10 17 15 12 10 7"/>
        <line x1="15" y1="12" x2="3" y2="12"/>
      </svg>
      Link Account
    </button>
  </form>

  <hr class="divider">

  <div class="steps">
    <div class="step">
      <div class="step-n">1</div>
      <div class="step-t">
        <strong>Find your MA URL</strong><br>
        Open Music Assistant &rarr; Settings &rarr; Info.
        The URL shown there is what you paste above.
      </div>
    </div>
    <div class="step">
      <div class="step-n">2</div>
      <div class="step-t">
        <strong>Create a token</strong><br>
        Settings &rarr; User &rarr; Long-lived access tokens &rarr; click +&nbsp;Create.
        Copy the token and paste it above.
      </div>
    </div>
    <div class="step">
      <div class="step-n">3</div>
      <div class="step-t">
        <strong>Link and go</strong><br>
        Click <em>Link Account</em>. Then say
        <strong>"Alexa, open music player"</strong> and start playing.
      </div>
    </div>
  </div>
</div>
</body>
</html>"""


def _render_form(
    client_id: str = "",
    redirect_uri: str = "",
    state: str = "",
    response_type: str = "code",
    error: str = "",
    ma_url: str = "",
    player_id: str = "",
) -> str:
    if error:
        error_block = (
            '<div class="alert">'
            '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" '
            'stroke="currentColor" stroke-width="2.5" stroke-linecap="round">'
            '<circle cx="12" cy="12" r="10"/>'
            '<line x1="12" y1="8" x2="12" y2="12"/>'
            '<line x1="12" y1="16" x2="12.01" y2="16"/>'
            "</svg>"
            f"<span>{error}</span></div>"
        )
    else:
        error_block = ""

    return _FORM_TEMPLATE.format(
        client_id=client_id,
        redirect_uri=redirect_uri,
        state=state,
        response_type=response_type,
        error_block=error_block,
        ma_url=ma_url,
        player_id=player_id,
    )


# ── Routes ────────────────────────────────────────────────────────────────────


@router.get("/authorize", summary="Account linking — show setup form")
async def authorize_get(
    response_type: str = "code",
    client_id: str = "",
    redirect_uri: str = "",
    state: str = "",
    scope: str = "",
) -> HTMLResponse:
    return HTMLResponse(
        _render_form(
            client_id=client_id,
            redirect_uri=redirect_uri,
            state=state,
            response_type=response_type,
        )
    )


@router.post("/authorize", summary="Account linking — handle form submission")
async def authorize_post(
    client_id: str = Form(""),
    redirect_uri: str = Form(""),
    state: str = Form(""),
    response_type: str = Form("code"),
    ma_url: str = Form(...),
    ma_token: str = Form(...),
    player_id: str = Form(""),
) -> HTMLResponse | RedirectResponse:
    from app.main import get_settings

    settings = get_settings()

    # ── Normalise MA URL ──────────────────────────────────────────────────
    ma_url = ma_url.strip().rstrip("/")
    if not ma_url.startswith(("http://", "https://")):
        return HTMLResponse(
            _render_form(
                client_id=client_id,
                redirect_uri=redirect_uri,
                state=state,
                response_type=response_type,
                error="Music Assistant URL must start with http:// or https://",
                ma_url=ma_url,
                player_id=player_id,
            )
        )

    # ── Test MA connectivity (fast 5 s check) ─────────────────────────────
    logger.info("Account linking: testing MA connectivity at %s", ma_url)
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.post(
                f"{ma_url}/api",
                json={"message_id": "link-test", "command": "server/info", "args": {}},
                headers={"Authorization": f"Bearer {ma_token}"},
            )
            if resp.status_code == 401:
                raise ValueError("Token rejected by Music Assistant (401 Unauthorized)")
            if resp.status_code == 403:
                raise ValueError("Token has insufficient permissions (403 Forbidden)")
            resp.raise_for_status()
            info = resp.json().get("result", {})
            version = info.get("server_version") or info.get("version") or "?"
            logger.info("Account linking: MA reachable version=%s", version)
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        logger.warning("Account linking: cannot reach MA at %s: %s", ma_url, exc)
        return HTMLResponse(
            _render_form(
                client_id=client_id,
                redirect_uri=redirect_uri,
                state=state,
                response_type=response_type,
                error=(
                    f"Cannot reach Music Assistant at <strong>{ma_url}</strong>. "
                    "Check the URL and make sure the server is running. "
                    "If using a local IP, ensure this service is on the same network."
                ),
                ma_url=ma_url,
                player_id=player_id,
            )
        )
    except ValueError as exc:
        return HTMLResponse(
            _render_form(
                client_id=client_id,
                redirect_uri=redirect_uri,
                state=state,
                response_type=response_type,
                error=str(exc),
                ma_url=ma_url,
                player_id=player_id,
            )
        )
    except Exception as exc:
        logger.warning("Account linking: MA test failed: %s", exc)
        return HTMLResponse(
            _render_form(
                client_id=client_id,
                redirect_uri=redirect_uri,
                state=state,
                response_type=response_type,
                error=f"Could not connect to Music Assistant: {exc}",
                ma_url=ma_url,
                player_id=player_id,
            )
        )

    # ── Store auth code and redirect to Alexa ────────────────────────────
    code = await storage.create_auth_code(
        path=settings.db_path,
        ma_url=ma_url,
        ma_token=ma_token,
        player_id=player_id.strip(),
        redirect_uri=redirect_uri,
        state=state,
    )
    logger.info("Account linking: auth code issued, redirecting back to Alexa")

    sep = "&" if "?" in redirect_uri else "?"
    destination = f"{redirect_uri}{sep}code={code}&state={state}"
    return RedirectResponse(url=destination, status_code=302)


@router.post("/token", summary="Account linking — exchange code for access token")
async def token_exchange(
    request: Request,
    grant_type: str = Form(""),
    code: str = Form(""),
    refresh_token: str = Form(""),
    client_id: str = Form(""),
    client_secret: str = Form(""),
    redirect_uri: str = Form(""),
) -> JSONResponse:
    from app.main import get_settings

    settings = get_settings()

    # ── Extract client credentials — support HTTP Basic auth ─────────────
    if not (client_id and client_secret):
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Basic "):
            try:
                decoded = base64.b64decode(auth_header[6:]).decode("utf-8")
                client_id, _, client_secret = decoded.partition(":")
            except Exception:
                pass

    # ── Validate client credentials ───────────────────────────────────────
    if not settings.oauth_client_secret:
        logger.error("ECHO_OAUTH_CLIENT_SECRET is not set — token exchange will always fail")
        return JSONResponse(status_code=500, content={"error": "server_misconfiguration"})

    if client_id != settings.oauth_client_id or client_secret != settings.oauth_client_secret:
        logger.warning(
            "Token exchange: invalid client credentials client_id=%s", client_id
        )
        return JSONResponse(status_code=401, content={"error": "invalid_client"})

    # ── authorization_code grant ──────────────────────────────────────────
    if grant_type == "authorization_code":
        if not code:
            return JSONResponse(
                status_code=400,
                content={"error": "invalid_request", "error_description": "code is required"},
            )
        result = await storage.exchange_code_for_token(settings.db_path, code)
        if not result:
            logger.warning("Token exchange: code expired or invalid")
            return JSONResponse(
                status_code=400,
                content={
                    "error": "invalid_grant",
                    "error_description": "Code expired or already used. Please link again.",
                },
            )
        logger.info("Token exchange: new access token issued")
        return JSONResponse(
            {
                "access_token": result["access_token"],
                "token_type": "bearer",
                # Same UUID is re-used as refresh_token for simplicity
                "refresh_token": result["access_token"],
                # Large TTL — tokens are permanent (user re-links to change config)
                "expires_in": 315360000,
            }
        )

    # ── refresh_token grant ───────────────────────────────────────────────
    if grant_type == "refresh_token":
        if not refresh_token:
            return JSONResponse(
                status_code=400,
                content={"error": "invalid_request", "error_description": "refresh_token is required"},
            )
        config = await storage.get_user_config(settings.db_path, refresh_token)
        if not config:
            logger.warning("Token refresh: token not found in DB — forcing re-link")
            return JSONResponse(
                status_code=400,
                content={
                    "error": "invalid_grant",
                    "error_description": "Token revoked. Please re-link in the Alexa app.",
                },
            )
        logger.info("Token refresh: returning existing token")
        return JSONResponse(
            {
                "access_token": refresh_token,
                "token_type": "bearer",
                "refresh_token": refresh_token,
                "expires_in": 315360000,
            }
        )

    return JSONResponse(
        status_code=400,
        content={"error": "unsupported_grant_type"},
    )
