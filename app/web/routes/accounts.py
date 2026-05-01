"""Account management routes: CRUD operations."""
from __future__ import annotations

from fastapi import APIRouter, Request, Depends, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.web.dependencies import get_account_manager
from app.core.account_manager import AccountManager, AccountManagerError


router = APIRouter()
templates = Jinja2Templates(directory="app/web/templates")


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
