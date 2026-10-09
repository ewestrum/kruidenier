"""HTML routes (Jinja2 + HTMX). Mobile first; every query is scoped to the user's household."""

import logging
from datetime import UTC, date, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.ah.client import HttpAhClient
from app.ah.errors import AhAuthError, AhError, AhHttpError, AhSchemaError
from app.ah.token_crypto import TokenCipher
from app.config import get_settings
from app.db.models import Household, Product, ProductFamily
from app.domain.feedback import FeedbackKind
from app.services import bonus, evaluation, family_admin, household_admin, push, users, week
from app.services.accounts import (
    client_for,
    get_or_create_household,
    order_account,
    save_account,
    shared_limiter,
)
from app.services.autopilot import autopilot_settings
from app.services.notify import Message, notifier_for
from app.services.planner import build_draft_plan, load_family, recompute_stats
from app.services.price_history import build_chart, price_series
from app.services.receipts import SETTING as RECEIPTS_SETTING
from app.services.receipts import receipts_enabled
from app.services.throttle import LOGIN_THROTTLE
from app.web.deps import AdminUser, CurrentUser, Db, check_csrf, get_sessions, render

Sessions = Annotated[sessionmaker[Session], Depends(get_sessions)]

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
    address = request.client.host if request.client else "?"
    wait = LOGIN_THROTTLE.wait_seconds(email, address)
    if wait:
        minutes = max(1, round(wait / 60))
        response = render(
            request,
            "login.html",
            email=email,
            error=f"Te veel mislukte pogingen. Probeer het over {minutes} minuten opnieuw.",
        )
        response.status_code = 429
        return response
    user = users.authenticate(db, email, password)
    if user is None:
        LOGIN_THROTTLE.failed(email, address)
        return render(
            request, "login.html", email=email, error="E-mailadres of wachtwoord klopt niet."
        )
    LOGIN_THROTTLE.succeeded(email, address)
    request.session.clear()  # new session id contents on login
    request.session["uid"] = user.id
    return _redirect("/week")


@router.get("/account", response_class=HTMLResponse)
def account_page(request: Request, user: CurrentUser, notice: str = "") -> Response:
    return render(request, "account.html", user, notice=notice)


@router.post("/account/password", response_class=HTMLResponse)
def account_password(
    request: Request,
    db: Db,
    user: CurrentUser,
    current: Annotated[str, Form()],
    new: Annotated[str, Form()],
    repeat: Annotated[str, Form()],
) -> Response:
    try:
        users.change_password(user, current=current, new=new, repeat=repeat)
    except users.UserError as e:
        return render(request, "account.html", user, error=str(e))
    return _redirect("/account?notice=Je wachtwoord is gewijzigd.")


@router.post("/logout")
def logout(request: Request) -> Response:
    request.session.clear()
    return _redirect("/login")


@router.get("/")
def home() -> Response:
    return _redirect("/week")


# --- Deze week -----------------------------------------------------------------


@router.get("/week", response_class=HTMLResponse)
def week_page(
    request: Request, db: Db, user: CurrentUser, notice: str = "", error: str = ""
) -> Response:
    view = week.week_view(db, user.household_id)
    pushes = push.push_actions(db, user.household_id, view.plan.id) if view.plan else []
    pilot = autopilot_settings(_household(db, user.household_id))
    evaluations = evaluation.recent_evaluations(db, user.household_id, today=date.today(), limit=4)
    return render(
        request,
        "week.html",
        user,
        view=view,
        pushes=pushes,
        autopilot=pilot.enabled,
        evaluations=evaluations,
        autopilot_hours=f"{pilot.hours_before:g}",
        has_account=order_account(db, user.household_id) is not None,
        today=date.today(),
        nav="week",
        notice=notice,
        error=error,
    )


def _ah_failure_text(e: AhError) -> str:
    if isinstance(e, AhSchemaError):
        return (
            "AH gaf een onverwacht antwoord, dus Kruidenier is gestopt. Kijk in de AH-app "
            "wat er in je bestelling staat."
        )
    if isinstance(e, AhAuthError):
        return "De koppeling met AH werkt niet meer. Koppel je account opnieuw bij Instellingen."
    return "AH was niet bereikbaar. Probeer het over een paar minuten opnieuw."


@router.post("/week/push")
async def push_week(db: Db, user: CurrentUser, sessions: Sessions) -> Response:
    account = order_account(db, user.household_id)
    if account is None:
        return _redirect("/week?error=Koppel eerst een AH-account bij Instellingen.")
    settings = get_settings()
    try:
        async with client_for(
            account, settings=settings, cipher=TokenCipher(settings.fernet_key), sessions=sessions
        ) as client:
            result = await push.push_plan(
                client,
                db,
                household_id=user.household_id,
                actor=f"user:{user.email}",
                now=datetime.now(UTC),
            )
    except push.PushError as e:
        return _redirect(f"/week?error={e}")
    except AhError as e:
        log.warning("push failed: %s", e)
        return _redirect(f"/week?error={_ah_failure_text(e)}")
    if result.changed:
        notice = f"{len(result.changed)} producten in je AH-bestelling gezet."
    else:
        notice = "Alles stond al in je AH-bestelling; er is niets veranderd."
    if result.not_taken:
        notice += f" AH nam niet over: {', '.join(result.not_taken)}."
    return _redirect(f"/week?notice={notice}")


@router.post("/week/undo/{action_id}")
async def undo_week(db: Db, user: CurrentUser, sessions: Sessions, action_id: int) -> Response:
    account = order_account(db, user.household_id)
    if account is None:
        return _redirect("/week?error=Koppel eerst een AH-account bij Instellingen.")
    settings = get_settings()
    try:
        async with client_for(
            account, settings=settings, cipher=TokenCipher(settings.fernet_key), sessions=sessions
        ) as client:
            result = await push.undo_push(
                client,
                db,
                household_id=user.household_id,
                action_id=action_id,
                now=datetime.now(UTC),
            )
    except push.PushError as e:
        return _redirect(f"/week?error={e}")
    except AhError as e:
        log.warning("undo failed: %s", e)
        return _redirect(f"/week?error={_ah_failure_text(e)}")
    notice = f"Teruggedraaid: {len(result.reverted)} producten."
    if result.left_alone:
        notice += (
            f" Niet aangeraakt omdat ze sindsdien zijn aangepast: {', '.join(result.left_alone)}."
        )
    return _redirect(f"/week?notice={notice}")


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


BACK_TARGETS = {"/week", "/often", "/bonus"}  # never redirect to a user-supplied URL


@router.post("/week/add/{family_id}", response_class=HTMLResponse)
def add_to_week(
    db: Db, user: CurrentUser, family_id: int, next: Annotated[str, Form()] = "/week"
) -> Response:
    try:
        line = week.add_family_to_plan(db, household_id=user.household_id, family_id=family_id)
    except week.NotFound:
        return HTMLResponse("", status_code=404)
    back = next if next in BACK_TARGETS else "/week"
    if back == "/week":
        return _redirect("/week")
    title = db.get(Product, line.ah_product_id)
    return _redirect(f"{back}?notice={title.title if title else 'Product'} staat in het voorstel.")


@router.get("/often", response_class=HTMLResponse)
def often_page(request: Request, db: Db, user: CurrentUser, notice: str = "") -> Response:
    view = week.week_view(db, user.household_id, max_suggestions=None)
    return render(request, "often.html", user, view=view, notice=notice, nav="often")


@router.get("/bonus", response_class=HTMLResponse)
def bonus_page(request: Request, db: Db, user: CurrentUser, notice: str = "") -> Response:
    view = bonus.bonus_view(db, user.household_id, today=date.today())
    return render(request, "bonus.html", user, view=view, notice=notice, nav="bonus")


@router.post("/bonus/add/{offer_id}")
def bonus_add(db: Db, user: CurrentUser, offer_id: int) -> Response:
    try:
        line = bonus.add_offer_to_plan(db, household_id=user.household_id, offer_id=offer_id)
    except week.NotFound:
        return HTMLResponse("", status_code=404)
    product = db.get(Product, line.ah_product_id)
    return _redirect(
        f"/bonus?notice={product.title if product else 'Product'} staat in het voorstel."
    )


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
    row = db.get(ProductFamily, family_id)
    chart_product = load_family(db, row).order_product if row else None
    chart = (
        build_chart(price_series(db, chart_product.ah_id, today=date.today()))
        if chart_product
        else None
    )
    return render(
        request,
        "family.html",
        user,
        family=family,
        others=others,
        error=error,
        nav="families",
        chart=chart,
        chart_title=chart_product.title if chart_product else "",
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
    evaluations = evaluation.recent_evaluations(db, household.id, today=date.today())
    login_url = str(HttpAhClient.build_login_url(get_settings().ah_client_id, LOGIN_REDIRECT_URI))
    return render(
        request,
        "settings.html",
        user,
        nav="settings",
        household=household,
        fields=household_admin.FIELDS,
        order_fields=household_admin.ORDER_FIELDS,
        values=household_admin.settings_values(household),
        pauses=household_admin.list_pauses(db, household.id),
        members=household_admin.list_users(db, household.id),
        accounts=household_admin.list_accounts(db, household.id),
        login_url=login_url,
        notify=household_admin.notification_values(household),
        receipts_on=receipts_enabled(household),
        reminder_field=household_admin.REMINDER_FIELD,
        notify_configured=notifier_for(household, get_settings()).configured,
        evaluations=evaluations,
        autopilot_ready=evaluation.autopilot_ready(evaluations),
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


@router.post("/settings/notifications", response_class=HTMLResponse)
async def settings_notifications(request: Request, db: Db, user: AdminUser) -> Response:
    form = {k: str(v) for k, v in (await request.form()).items()}
    try:
        household_admin.update_notification_settings(_household(db, user.household_id), form)
    except household_admin.SettingsError as e:
        return _settings_page(request, db, user, error=str(e))
    return _redirect("/settings?notice=Meldingen opgeslagen.#meldingen")


@router.post("/settings/notifications/test")
async def settings_notifications_test(db: Db, user: AdminUser) -> Response:
    notifier = notifier_for(_household(db, user.household_id), get_settings())
    if not notifier.configured:
        return _redirect("/settings?notice=Vul eerst een meldingsadres in.#meldingen")
    ok = await notifier.send(
        Message("Kruidenier: testmelding", "Meldingen werken. Zo hoor je het als er iets is.")
    )
    text = "Testmelding verstuurd." if ok else "Testmelding kwam niet aan; controleer het adres."
    return _redirect(f"/settings?notice={text}#meldingen")


@router.post("/settings/receipts")
def settings_receipts(
    db: Db, user: AdminUser, enabled: Annotated[bool, Form()] = False
) -> Response:
    household = _household(db, user.household_id)
    household.settings_json = {**(household.settings_json or {}), RECEIPTS_SETTING: enabled}
    text = "Winkelaankopen tellen mee." if enabled else "Winkelaankopen tellen niet meer mee."
    return _redirect(f"/settings?notice={text}")


@router.post("/settings/ordering", response_class=HTMLResponse)
async def settings_ordering(request: Request, db: Db, user: AdminUser) -> Response:
    form = {k: str(v) for k, v in (await request.form()).items()}
    try:
        household_admin.update_order_settings(_household(db, user.household_id), form)
    except household_admin.SettingsError as e:
        return _settings_page(request, db, user, error=str(e))
    return _redirect("/settings?notice=Instellingen voor bestellen opgeslagen.")


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
