"""HTML routes (Jinja2 + HTMX). Mobile first; every query is scoped to the user's household."""

import logging
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import select

from app.ah.client import HttpAhClient
from app.ah.errors import AhError, AhHttpError
from app.ah.token_crypto import TokenCipher
from app.config import get_settings
from app.db.models import Household
from app.domain.feedback import FeedbackKind
from app.services import family_admin, household_admin, users, week
from app.services.accounts import get_or_create_household, save_account, shared_limiter
from app.services.planner import build_draft_plan, recompute_stats
from app.web.deps import AdminUser, CurrentUser, Db, check_csrf, render

log = logging.getLogger(__name__)
router = APIRouter(dependencies=[Depends(check_csrf)])

LOGIN_REDIRECT_URI = "appie://login-exit"


def _redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


def _household(db: Db, user_household_id: int) -> Household:
    household = db.get(Household, user_household_id)
    assert household is not None
    return household


# --- first run and login ------------------------------------------------------


@router.get("/setup", response_class=HTMLResponse)
def setup_form(request: Request, db: Db) -> Response:
    if users.has_users(db):
        return _redirect("/login")
    existing = db.scalars(select(Household.name)).all()
    return render(request, "setup.html", household_name=existing[0] if existing else "Thuis")


@router.post("/setup", response_class=HTMLResponse)
def setup(
    request: Request,
    db: Db,
    household_name: Annotated[str, Form()],
    email: Annotated[str, Form()],
    password: Annotated[str, Form()],
) -> Response:
    if users.has_users(db):
        return _redirect("/login")
    household = get_or_create_household(db, household_name.strip() or "Thuis")
    try:
        user = users.create_user(
            db, household=household, email=email, password=password, role="admin"
        )
    except users.UserError as e:
        return render(
            request, "setup.html", household_name=household_name, email=email, error=str(e)
        )
    request.session.clear()
    request.session["uid"] = user.id
    return _redirect("/week")


@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request, db: Db) -> Response:
    if not users.has_users(db):
        return _redirect("/setup")
    return render(request, "login.html")


@router.post("/login", response_class=HTMLResponse)
def login(
    request: Request,
    db: Db,
    email: Annotated[str, Form()],
    password: Annotated[str, Form()],
) -> Response:
    user = users.authenticate(db, email, password)
    if user is None:
        return render(
            request, "login.html", email=email, error="E-mailadres of wachtwoord klopt niet."
        )
    request.session.clear()  # new session id contents on login
    request.session["uid"] = user.id
    return _redirect("/week")


@router.post("/logout")
def logout(request: Request) -> Response:
    request.session.clear()
    return _redirect("/login")


@router.get("/")
def home() -> Response:
    return _redirect("/week")


# --- Deze week -----------------------------------------------------------------


@router.get("/week", response_class=HTMLResponse)
def week_page(request: Request, db: Db, user: CurrentUser) -> Response:
    view = week.week_view(db, user.household_id)
    return render(request, "week.html", user, view=view, today=date.today(), nav="week")


@router.post("/week/lines/{line_id}/{kind}", response_class=HTMLResponse)
def line_feedback(
    request: Request, db: Db, user: CurrentUser, line_id: int, kind: FeedbackKind
) -> Response:
    try:
        line = week.give_feedback(
            db, household_id=user.household_id, user_id=user.id, line_id=line_id, kind=kind
        )
    except week.NotFound:
        return HTMLResponse("", status_code=404)
    view = week.week_view(db, user.household_id)
    current = next((v for v in view.lines if line and v.id == line.id), None)
    return render(request, "_feedback_result.html", user, view=view, line=current, kind=kind.value)


@router.post("/week/add/{family_id}", response_class=HTMLResponse)
def add_to_week(request: Request, db: Db, user: CurrentUser, family_id: int) -> Response:
    try:
        week.add_family_to_plan(db, household_id=user.household_id, family_id=family_id)
    except week.NotFound:
        return HTMLResponse("", status_code=404)
    return _redirect("/week")


@router.post("/week/rebuild")
def rebuild_week(db: Db, user: CurrentUser) -> Response:
    household = _household(db, user.household_id)
    plan = week.latest_plan(db, household.id)
    if plan is not None:
        today = date.today()
        recompute_stats(db, household, today=today)
        build_draft_plan(
            db,
            household,
            delivery_date=plan.delivery_date,
            today=today,
            ah_order_id=plan.ah_order_id,
            cutoff=plan.cutoff,
        )
    return _redirect("/week")


# --- Families ------------------------------------------------------------------


@router.get("/families", response_class=HTMLResponse)
def families_page(request: Request, db: Db, user: CurrentUser, q: str = "") -> Response:
    families = family_admin.list_families(db, user.household_id, q)
    template = "_family_list.html" if request.headers.get("hx-request") else "families.html"
    return render(request, template, user, families=families, q=q, nav="families")


@router.get("/families/{family_id}", response_class=HTMLResponse)
def family_page(
    request: Request, db: Db, user: CurrentUser, family_id: int, error: str = ""
) -> Response:
    try:
        family = family_admin.get_family(db, user.household_id, family_id)
    except family_admin.FamilyError:
        return _redirect("/families")
    others = [
        f
        for f in family_admin.list_families(db, user.household_id)
        if f.id != family.id and f.base_unit == family.base_unit
    ]
    return render(
        request, "family.html", user, family=family, others=others, error=error, nav="families"
    )


def _family_action(family_id: int, action: object) -> Response:
    try:
        if callable(action):
            action()
    except family_admin.FamilyError as e:
        return _redirect(f"/families/{family_id}?error={e}")
    return _redirect(f"/families/{family_id}")


@router.post("/families/{family_id}/rename")
def family_rename(
    db: Db, user: CurrentUser, family_id: int, name: Annotated[str, Form()]
) -> Response:
    return _family_action(
        family_id, lambda: family_admin.rename(db, user.household_id, family_id, name)
    )


@router.post("/families/{family_id}/flags")
def family_flags(
    db: Db,
    user: CurrentUser,
    family_id: int,
    flag: Annotated[str, Form()],
    value: Annotated[bool, Form()],
) -> Response:
    kwargs = {"pinned": value} if flag == "pinned" else {"excluded": value}
    return _family_action(
        family_id,
        lambda: family_admin.set_flags(db, user.household_id, family_id, **kwargs),
    )


@router.post("/families/{family_id}/preferred")
def family_preferred(
    db: Db, user: CurrentUser, family_id: int, product_id: Annotated[int, Form()]
) -> Response:
    return _family_action(
        family_id,
        lambda: family_admin.set_preferred(db, user.household_id, family_id, product_id),
    )


@router.post("/families/{family_id}/merge")
def family_merge(
    db: Db, user: CurrentUser, family_id: int, source_id: Annotated[int, Form()]
) -> Response:
    return _family_action(
        family_id, lambda: family_admin.merge(db, user.household_id, family_id, [source_id])
    )


@router.post("/families/{family_id}/split")
def family_split(
    db: Db, user: CurrentUser, family_id: int, product_id: Annotated[int, Form()]
) -> Response:
    try:
        new = family_admin.split(db, user.household_id, family_id, product_id)
    except family_admin.FamilyError as e:
        return _redirect(f"/families/{family_id}?error={e}")
    return _redirect(f"/families/{new.id}")


# --- Instellingen ------------------------------------------------------------------


def _settings_page(
    request: Request, db: Db, user: AdminUser, *, error: str = "", notice: str = ""
) -> Response:
    household = _household(db, user.household_id)
    login_url = str(HttpAhClient.build_login_url(get_settings().ah_client_id, LOGIN_REDIRECT_URI))
    return render(
        request,
        "settings.html",
        user,
        nav="settings",
        household=household,
        fields=household_admin.FIELDS,
        values=household_admin.settings_values(household),
        pauses=household_admin.list_pauses(db, household.id),
        members=household_admin.list_users(db, household.id),
        accounts=household_admin.list_accounts(db, household.id),
        login_url=login_url,
        today=date.today(),
        error=error,
        notice=notice,
    )


@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, db: Db, user: AdminUser, notice: str = "") -> Response:
    return _settings_page(request, db, user, notice=notice)


@router.post("/settings/household", response_class=HTMLResponse)
async def settings_household(request: Request, db: Db, user: AdminUser) -> Response:
    form = {k: str(v) for k, v in (await request.form()).items()}
    try:
        household_admin.update_settings(_household(db, user.household_id), form)
    except household_admin.SettingsError as e:
        return _settings_page(request, db, user, error=str(e))
    return _redirect("/settings?notice=Instellingen opgeslagen.")


@router.post("/settings/pauses", response_class=HTMLResponse)
def settings_add_pause(
    request: Request,
    db: Db,
    user: AdminUser,
    start: Annotated[date, Form()],
    end: Annotated[date, Form()],
) -> Response:
    try:
        household_admin.add_pause(db, user.household_id, start, end)
    except household_admin.SettingsError as e:
        return _settings_page(request, db, user, error=str(e))
    return _redirect("/settings?notice=Vakantie toegevoegd.")


@router.post("/settings/pauses/{pause_id}/delete", response_class=HTMLResponse)
def settings_delete_pause(request: Request, db: Db, user: AdminUser, pause_id: int) -> Response:
    try:
        household_admin.delete_pause(db, user.household_id, pause_id)
    except household_admin.SettingsError as e:
        return _settings_page(request, db, user, error=str(e))
    return _redirect("/settings?notice=Vakantie verwijderd.")


@router.post("/settings/users", response_class=HTMLResponse)
def settings_add_user(
    request: Request,
    db: Db,
    user: AdminUser,
    email: Annotated[str, Form()],
    password: Annotated[str, Form()],
    role: Annotated[str, Form()] = "member",
) -> Response:
    try:
        users.create_user(
            db,
            household=_household(db, user.household_id),
            email=email,
            password=password,
            role=role,
        )
    except users.UserError as e:
        return _settings_page(request, db, user, error=str(e))
    return _redirect("/settings?notice=Gebruiker toegevoegd.")


@router.post("/settings/users/{user_id}/delete", response_class=HTMLResponse)
def settings_delete_user(request: Request, db: Db, user: AdminUser, user_id: int) -> Response:
    try:
        household_admin.delete_user(db, user.household_id, user_id, acting_user_id=user.id)
    except household_admin.SettingsError as e:
        return _settings_page(request, db, user, error=str(e))
    return _redirect("/settings?notice=Gebruiker verwijderd.")


@router.post("/settings/ah", response_class=HTMLResponse)
async def settings_link_ah(
    request: Request,
    db: Db,
    user: AdminUser,
    redirect_url: Annotated[str, Form()],
    label: Annotated[str, Form()] = "AH",
) -> Response:
    settings = get_settings()
    code = HttpAhClient.extract_code(redirect_url)
    if not code:
        return _settings_page(
            request, db, user, error="Plak de hele regel 'appie://login-exit?code=…'."
        )
    try:
        cipher = TokenCipher(settings.fernet_key)
        async with HttpAhClient(
            client_id=settings.ah_client_id,
            client_version=settings.ah_client_version,
            limiter=shared_limiter(settings),
        ) as client:
            tokens = await client.exchange_code(code)
    except AhHttpError as e:
        log.warning("AH code exchange failed: %s", e)
        return _settings_page(
            request,
            db,
            user,
            error="AH accepteerde deze code niet. Een code werkt maar één keer en verloopt "
            "binnen een paar minuten: log opnieuw in en plak de nieuwe regel.",
        )
    except (AhError, ValueError) as e:
        log.warning("AH link failed: %s", e)
        return _settings_page(request, db, user, error=f"Koppelen mislukt: {e}")
    save_account(
        db,
        household=_household(db, user.household_id),
        label=label.strip() or "AH",
        tokens=tokens,
        cipher=cipher,
    )
    return _redirect("/settings?notice=AH-account gekoppeld.")
