"""Account management routes: CRUD operations."""
from __future__ import annotations

import os
import secrets
from fastapi import APIRouter, Request, Depends, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from google_auth_oauthlib.flow import Flow

from app.web.dependencies import get_account_manager
from app.core.account_manager import AccountManager, AccountManagerError


router = APIRouter()
templates = Jinja2Templates(directory="app/web/templates")

# Google adds `openid` when email/profile are requested, so relax scope matching.
os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")

# OAuth configuration
SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
    "openid",
]
REDIRECT_URI = "http://127.0.0.1:8000/accounts/oauth/callback"

# Store states temporarily (in production, use Redis or database)
_oauth_states = {}


@router.get("/", response_class=HTMLResponse)
def list_accounts(
    request: Request,
    account_mgr: AccountManager = Depends(get_account_manager),
):
    """Render accounts list page.

    Args:
        request: FastAPI request
        account_mgr: Account manager dependency

    Returns:
        Accounts list HTML page
    """
    accounts = account_mgr.list_accounts()

    return templates.TemplateResponse(
        request=request,
        name="accounts.html",
        context={
            "accounts": accounts,
        }
    )


@router.get("/new", response_class=HTMLResponse)
def new_account_form(request: Request):
    """Render new account form.

    Args:
        request: FastAPI request

    Returns:
        Account form HTML page
    """
    return templates.TemplateResponse(
        request=request,
        name="account_form.html",
        context={
            "account": None,
            "error": None,
        }
    )


@router.post("/new")
def create_account(
    name: str = Form(...),
    email: str = Form(...),
    client_id: str = Form(...),
    client_secret: str = Form(...),
    refresh_token: str = Form(...),
    daily_quota: int = Form(10000),
    account_mgr: AccountManager = Depends(get_account_manager),
):
    """Create new account.

    Args:
        name: Account name
        email: Email address
        client_id: OAuth client ID
        client_secret: OAuth client secret
        refresh_token: OAuth refresh token
        daily_quota: Daily quota limit
        account_mgr: Account manager dependency

    Returns:
        Redirect to accounts list on success
    """
    try:
        account_mgr.add_account(
            name=name,
            email=email,
            client_id=client_id,
            client_secret=client_secret,
            refresh_token=refresh_token,
            daily_quota=daily_quota,
        )
        return RedirectResponse(url="/accounts", status_code=303)
    except AccountManagerError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/oauth/start")
def start_oauth_flow(request: Request):
    """Start OAuth flow for adding account.

    Returns:
        Redirect to Google OAuth consent screen
    """
    client_id = os.getenv("YOUTUBE_WEB_CLIENT_ID")
    client_secret = os.getenv("YOUTUBE_WEB_CLIENT_SECRET")

    if not client_id or not client_secret:
        raise HTTPException(
            status_code=500,
            detail="OAuth credentials not configured. Set YOUTUBE_WEB_CLIENT_ID and YOUTUBE_WEB_CLIENT_SECRET in .env"
        )

    # Generate a random PKCE code_verifier (43-128 chars, unreserved set)
    code_verifier = secrets.token_urlsafe(64)

    # Create OAuth flow
    flow = Flow.from_client_config(
        client_config={
            "web": {
                "client_id": client_id,
                "client_secret": client_secret,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": [REDIRECT_URI],
            }
        },
        scopes=SCOPES,
        redirect_uri=REDIRECT_URI,
        code_verifier=code_verifier,
    )

    # Generate state for CSRF protection
    state = secrets.token_urlsafe(32)
    authorization_url, _ = flow.authorization_url(
        access_type="offline",
        prompt="consent",
        state=state,
    )

    # Store flow data including PKCE code_verifier in temporary storage
    _oauth_states[state] = {
        "client_id": client_id,
        "client_secret": client_secret,
        "code_verifier": code_verifier,
    }

    return RedirectResponse(url=authorization_url)


@router.get("/oauth/callback")
def oauth_callback(
    request: Request,
    state: str,
    code: str | None = None,
    error: str | None = None,
    account_mgr: AccountManager = Depends(get_account_manager),
):
    """Handle OAuth callback from Google.

    Args:
        request: FastAPI request
        state: CSRF state token
        code: Authorization code from Google
        error: Error message if authorization failed
        account_mgr: Account manager dependency

    Returns:
        Redirect to accounts list with success/error message
    """
    # Check for errors
    if error:
        raise HTTPException(status_code=400, detail=f"OAuth authorization failed: {error}")

    # Verify state
    if state not in _oauth_states:
        raise HTTPException(status_code=400, detail="Invalid OAuth state. Please try again.")

    oauth_data = _oauth_states.pop(state)

    # Exchange authorization code for tokens
    flow = Flow.from_client_config(
        client_config={
            "web": {
                "client_id": oauth_data["client_id"],
                "client_secret": oauth_data["client_secret"],
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": [REDIRECT_URI],
            }
        },
        scopes=SCOPES,
        redirect_uri=REDIRECT_URI,
        code_verifier=oauth_data["code_verifier"],
    )

    flow.fetch_token(code=code)
    credentials = flow.credentials

    # Get user info to extract email
    import requests
    userinfo_response = requests.get(
        "https://www.googleapis.com/oauth2/v2/userinfo",
        headers={"Authorization": f"Bearer {credentials.token}"},
    )
    if not userinfo_response.ok:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Failed to fetch user info from Google "
                f"({userinfo_response.status_code}): {userinfo_response.text}"
            ),
        )
    userinfo = userinfo_response.json()
    email = userinfo.get("email")
    if not email:
        raise HTTPException(
            status_code=400,
            detail=f"Google userinfo response missing email: {userinfo}",
        )

    # Generate account name from email
    account_name = email.split("@")[0].replace(".", " ").title()

    # Add account to database
    try:
        account_mgr.add_account(
            name=account_name,
            email=email,
            client_id=oauth_data["client_id"],
            client_secret=oauth_data["client_secret"],
            refresh_token=credentials.refresh_token,
            daily_quota=10000,
        )
        return RedirectResponse(url="/accounts?success=oauth", status_code=303)
    except AccountManagerError as exc:
        # If name exists, try with suffix
        try:
            import time
            account_name_unique = f"{account_name} ({int(time.time()) % 10000})"
            account_mgr.add_account(
                name=account_name_unique,
                email=email,
                client_id=oauth_data["client_id"],
                client_secret=oauth_data["client_secret"],
                refresh_token=credentials.refresh_token,
                daily_quota=10000,
            )
            return RedirectResponse(url="/accounts?success=oauth", status_code=303)
        except AccountManagerError as exc:
            raise HTTPException(status_code=400, detail=str(exc))


@router.post("/{account_id}/toggle")
def toggle_account(
    account_id: int,
    account_mgr: AccountManager = Depends(get_account_manager),
):
    """Toggle account active status.

    Args:
        account_id: Account ID
        account_mgr: Account manager dependency

    Returns:
        Redirect to accounts list
    """
    try:
        account_mgr.toggle_active(account_id)
        return RedirectResponse(url="/accounts", status_code=303)
    except AccountManagerError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
