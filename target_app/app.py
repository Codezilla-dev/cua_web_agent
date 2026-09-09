"""Routes for the legacy-hostile target app.

Flow: login -> member search (in an iframe) -> member detail -> transfer form
-> review -> confirm.

Auth is a cookie holding the operator id. That is not security; it is enough to
make the session real, so that a page can be reached only after signing in and a
later phase has something to expire.

Every route accepts `?inject=` (see faults.py). Timing faults are applied before
any work; page-shaped faults change what is rendered.
"""

import secrets
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from target_app.data import (
    OPERATOR_PASSWORD,
    OPERATOR_USERNAME,
    find_members,
    get_member,
)
from target_app.faults import Fault, apply_timing_fault, parse_fault

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

SESSION_COOKIE = "console_session"

app = FastAPI(title="Northgate Servicing Console", docs_url=None, redoc_url=None)


def _is_signed_in(request: Request) -> bool:
    return bool(request.cookies.get(SESSION_COOKIE))


def _resolve_fault(inject: str | None) -> Fault | None:
    """Parse the `?inject=` flag and, for the timing faults, block right away.

    Every route pairs these two calls, so they are one call here instead of two
    duplicated at each of the eight route handlers below.
    """
    fault = parse_fault(inject)
    apply_timing_fault(fault)
    return fault


def _render(request: Request, template: str, fault: Fault | None, **context) -> HTMLResponse:
    """Render with the two things every template needs: request and fault."""
    return TEMPLATES.TemplateResponse(
        request=request, name=template, context={"fault": fault, **context}
    )


def _notice(
    request: Request, fault: Fault | None, heading: str, message: str, tone: str, status: int
) -> HTMLResponse:
    """The shared notice page, used for both not-found and denied."""
    palette = {
        "warning": ("#fff6e0", "#ccbb88", "#886611"),
        "error": ("#ffe8e8", "#cc8888", "#aa2222"),
    }
    background, border, colour = palette[tone]
    response = _render(
        request,
        "notice.html",
        fault,
        heading=heading,
        message=message,
        background=background,
        border=border,
        colour=colour,
    )
    response.status_code = status
    return response


def _sign_in_redirect() -> RedirectResponse:
    return RedirectResponse(url="/login", status_code=303)


def _validate_transfer(to_account: str, amount: str) -> str | None:
    """Return a message when the form should be re-rendered with an error."""
    if not to_account.strip():
        return "Destination account is required."
    try:
        parsed = float(amount.replace(",", "").strip())
    except ValueError:
        return "Amount must be a number."
    if parsed <= 0:
        return "Amount must be greater than zero."
    return None


@app.get("/")
def index() -> RedirectResponse:
    return RedirectResponse(url="/login", status_code=303)


@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request, inject: Annotated[str | None, Query()] = None) -> HTMLResponse:
    fault = _resolve_fault(inject)
    return _render(request, "login.html", fault, error=None)


@app.post("/login", response_class=HTMLResponse)
def login_submit(
    request: Request,
    username: Annotated[str, Form()] = "",
    password: Annotated[str, Form()] = "",
    inject: Annotated[str | None, Query()] = None,
):
    fault = _resolve_fault(inject)

    if username != OPERATOR_USERNAME or password != OPERATOR_PASSWORD:
        response = _render(
            request, "login.html", fault, error="Operator ID or passcode not recognised."
        )
        response.status_code = 401
        return response

    response = RedirectResponse(url="/search", status_code=303)
    response.set_cookie(SESSION_COOKIE, username, httponly=True)
    return response


@app.get("/logout")
def logout() -> RedirectResponse:
    response = RedirectResponse(url="/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE)
    return response


@app.get("/search", response_class=HTMLResponse)
def search_page(
    request: Request,
    q: Annotated[str | None, Query()] = None,
    inject: Annotated[str | None, Query()] = None,
):
    fault = _resolve_fault(inject)
    if not _is_signed_in(request):
        return _sign_in_redirect()

    # The outer page carries the query through to the frame so a deep link works.
    suffix = f"?q={q}" if q else ""
    return _render(request, "search.html", fault, query_suffix=suffix)


@app.get("/search_frame", response_class=HTMLResponse)
def search_frame(
    request: Request,
    q: Annotated[str | None, Query()] = None,
    inject: Annotated[str | None, Query()] = None,
):
    fault = _resolve_fault(inject)
    if not _is_signed_in(request):
        return _sign_in_redirect()

    # `notfound` forces the empty-result branch regardless of the query. It is a
    # 200 with a message: a business outcome, not an error.
    results = [] if fault is Fault.NOTFOUND else find_members(q or "")
    return _render(
        request,
        "search_frame.html",
        fault,
        query=q,
        searched=bool(q) or fault is Fault.NOTFOUND,
        results=results,
    )


@app.get("/member/{member_id}", response_class=HTMLResponse)
def member_detail(
    request: Request, member_id: str, inject: Annotated[str | None, Query()] = None
):
    fault = _resolve_fault(inject)
    if not _is_signed_in(request):
        return _sign_in_redirect()

    if fault is Fault.DENIED:
        return _notice(
            request,
            fault,
            "Access Denied",
            "Your operator role does not permit viewing this member record.",
            "error",
            403,
        )

    member = None if fault is Fault.NOTFOUND else get_member(member_id)
    if member is None:
        return _notice(
            request,
            fault,
            "Record Not Found",
            f"No member record exists for ID {member_id}.",
            "warning",
            200,
        )

    return _render(request, "member.html", fault, member=member)


@app.get("/transfer", response_class=HTMLResponse)
def transfer_form(
    request: Request,
    from_: Annotated[str | None, Query(alias="from")] = None,
    inject: Annotated[str | None, Query()] = None,
):
    fault = _resolve_fault(inject)
    if not _is_signed_in(request):
        return _sign_in_redirect()

    member = get_member(from_ or "")
    if member is None:
        return _notice(
            request,
            fault,
            "Record Not Found",
            "Cannot start a transfer without a valid member record.",
            "warning",
            200,
        )
    return _render(request, "transfer.html", fault, member=member, error=None)


@app.post("/transfer", response_class=HTMLResponse)
def transfer_review(
    request: Request,
    member_id: Annotated[str, Form()] = "",
    from_account: Annotated[str, Form()] = "",
    to_account: Annotated[str, Form()] = "",
    amount: Annotated[str, Form()] = "",
    inject: Annotated[str | None, Query()] = None,
):
    fault = _resolve_fault(inject)
    if not _is_signed_in(request):
        return _sign_in_redirect()

    member = get_member(member_id)
    if member is None:
        return _notice(
            request,
            fault,
            "Record Not Found",
            "That member record no longer exists.",
            "warning",
            200,
        )

    # Validation errors re-render the form. A real console does the same, and the
    # agent has to notice that it did not advance.
    error = _validate_transfer(to_account, amount)
    if error:
        return _render(request, "transfer.html", fault, member=member, error=error)

    return _render(
        request,
        "review.html",
        fault,
        member_id=member_id,
        from_account=from_account,
        to_account=to_account,
        amount=amount,
    )


@app.post("/transfer/confirm", response_class=HTMLResponse)
def transfer_confirm(
    request: Request,
    member_id: Annotated[str, Form()] = "",
    from_account: Annotated[str, Form()] = "",
    to_account: Annotated[str, Form()] = "",
    amount: Annotated[str, Form()] = "",
    inject: Annotated[str | None, Query()] = None,
):
    fault = _resolve_fault(inject)
    if not _is_signed_in(request):
        return _sign_in_redirect()

    if fault is Fault.DENIED:
        return _notice(
            request,
            fault,
            "Access Denied",
            "Transfer authority is limited to supervisors.",
            "error",
            403,
        )

    return _render(
        request,
        "done.html",
        fault,
        member_id=member_id,
        from_account=from_account,
        to_account=to_account,
        amount=amount,
        confirmation=f"NX-{secrets.token_hex(3).upper()}",
    )
