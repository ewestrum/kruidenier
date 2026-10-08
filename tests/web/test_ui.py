"""Web UI flows through the real app, on an in-memory database."""

import re
from collections.abc import Iterator
from datetime import date, timedelta

import pytest
import respx
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings
from app.db.models import (
    AhAccount,
    Household,
    Pause,
    Plan,
    PlanLine,
    Product,
    ProductFamily,
    Purchase,
    User,
)
from app.services.families import assign_families
from app.services.planner import build_draft_plan, recompute_stats
from app.services.users import create_user
from app.web.deps import get_sessions
from app.web.main import app

PASSWORD = "geheim-genoeg"
D0 = date.today() - timedelta(weeks=6)


@pytest.fixture
def client(sessions: sessionmaker[Session]) -> Iterator[TestClient]:
    app.dependency_overrides[get_sessions] = lambda: sessions
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def csrf_of(html: str) -> str:
    m = re.search(r'name="csrf" value="([^"]+)"', html) or re.search(
        r'"X-CSRF-Token": "([^"]+)"', html
    )
    assert m, "no csrf token on page"
    return m.group(1)


def make_household(
    sessions: sessionmaker[Session], name: str, email: str, role: str = "admin"
) -> int:
    with sessions.begin() as s:
        h = Household(name=name, settings_json={})
        s.add(h)
        s.flush()
        create_user(s, household=h, email=email, password=PASSWORD, role=role)
        return h.id


def login(client: TestClient, email: str) -> str:
    page = client.get("/login")
    r = client.post(
        "/login",
        data={"email": email, "password": PASSWORD, "csrf": csrf_of(page.text)},
        follow_redirects=False,
    )
    assert r.status_code == 303, r.text
    return csrf_of(client.get("/week").text)


def seed_plan(sessions: sessionmaker[Session], household_id: int, product_id: int = 1525) -> int:
    """Weekly milk + a rarely bought product; returns the milk plan line id."""
    with sessions.begin() as s:
        s.add(
            Product(
                ah_id=product_id,
                title="AH Halfvolle melk",
                brand="AH",
                unit_size_text="1 l",
                unit_amount=1000,
                unit="ml",
            )
        )
        s.add(Product(ah_id=product_id + 1, title="Unox Knaks", brand="Unox"))
        s.flush()
        for i in range(6):
            s.add(
                Purchase(
                    household_id=household_id,
                    ah_order_id=product_id * 10 + i,
                    ah_product_id=product_id,
                    qty=2,
                    delivered_at=D0 + timedelta(weeks=i),
                )
            )
        for i in range(2):
            s.add(
                Purchase(
                    household_id=household_id,
                    ah_order_id=product_id * 10 + i,
                    ah_product_id=product_id + 1,
                    qty=1,
                    delivered_at=D0 + timedelta(weeks=i),
                )
            )
        s.flush()
        assign_families(s, household_id)
        h = s.get(Household, household_id)
        assert h is not None
        recompute_stats(s, h, today=date.today())
        plan = build_draft_plan(
            s, h, delivery_date=date.today() + timedelta(days=2), today=date.today()
        )
        assert len(plan.lines) == 1
        return plan.lines[0].id


# --- first run & auth -------------------------------------------------------------


def test_first_visit_goes_to_setup_and_creates_admin(
    client: TestClient, sessions: sessionmaker[Session]
) -> None:
    r = client.get("/week", follow_redirects=True)
    assert r.url.path == "/setup"
    r = client.post(
        "/setup",
        data={
            "household_name": "Thuis",
            "email": "Ik@Example.nl",
            "password": PASSWORD,
            "csrf": csrf_of(r.text),
        },
        follow_redirects=True,
    )
    assert r.url.path == "/week" and "Nog geen concept" in r.text
    with sessions() as s:
        user = s.scalars(select(User)).one()
        assert user.email == "ik@example.nl" and user.role == "admin"
        assert user.pw_hash.startswith("scrypt$") and PASSWORD not in user.pw_hash
    assert client.get("/setup", follow_redirects=False).headers["location"] == "/login"


def test_setup_reuses_household_created_by_cli(
    client: TestClient, sessions: sessionmaker[Session]
) -> None:
    with sessions.begin() as s:
        s.add(Household(name="Thuis", settings_json={}))
    page = client.get("/setup")
    assert 'value="Thuis"' in page.text
    client.post(
        "/setup",
        data={
            "household_name": "Thuis",
            "email": "a@b.nl",
            "password": PASSWORD,
            "csrf": csrf_of(page.text),
        },
    )
    with sessions() as s:
        assert len(s.scalars(select(Household)).all()) == 1


def test_setup_rejects_short_password(client: TestClient) -> None:
    page = client.get("/setup")
    r = client.post(
        "/setup",
        data={
            "household_name": "Thuis",
            "email": "a@b.nl",
            "password": "kort",
            "csrf": csrf_of(page.text),
        },
    )
    assert "minstens 8 tekens" in r.text


def test_login_wrong_password(client: TestClient, sessions: sessionmaker[Session]) -> None:
    make_household(sessions, "Thuis", "a@b.nl")
    page = client.get("/login")
    r = client.post(
        "/login", data={"email": "a@b.nl", "password": "fout-fout", "csrf": csrf_of(page.text)}
    )
    assert "klopt niet" in r.text
    assert client.get("/week", follow_redirects=False).status_code == 303


def test_post_without_csrf_is_refused(client: TestClient, sessions: sessionmaker[Session]) -> None:
    make_household(sessions, "Thuis", "a@b.nl")
    login(client, "a@b.nl")
    r = client.post("/week/rebuild", follow_redirects=False)
    assert r.status_code == 403 and "verlopen" in r.text


def test_logout(client: TestClient, sessions: sessionmaker[Session]) -> None:
    make_household(sessions, "Thuis", "a@b.nl")
    token = login(client, "a@b.nl")
    client.post("/logout", data={"csrf": token})
    assert client.get("/week", follow_redirects=False).status_code == 303


def test_htmx_request_without_login_gets_hx_redirect(client: TestClient) -> None:
    r = client.get("/families", headers={"HX-Request": "true"})
    assert r.headers.get("HX-Redirect") == "/login"


# --- Deze week ---------------------------------------------------------------------


def test_week_shows_draft_lines_and_suggestions(
    client: TestClient, sessions: sessionmaker[Session]
) -> None:
    hid = make_household(sessions, "Thuis", "a@b.nl")
    seed_plan(sessions, hid)
    login(client, "a@b.nl")
    html = client.get("/week").text
    assert "AH Halfvolle melk" in html
    assert "Voorraad op rond" in html
    assert "Vaak gekocht, niet in dit voorstel" in html and "Knaks" in html
    assert "zet nog niets in je AH-bestelling" in html


def _line_qty(sessions: sessionmaker[Session], line_id: int) -> int | None:
    with sessions() as s:
        line = s.get(PlanLine, line_id)
        return line.qty if line else None


def test_feedback_more_and_less(client: TestClient, sessions: sessionmaker[Session]) -> None:
    hid = make_household(sessions, "Thuis", "a@b.nl")
    line_id = seed_plan(sessions, hid)
    token = login(client, "a@b.nl")
    before = _line_qty(sessions, line_id)
    assert before is not None
    r = client.post(
        f"/week/lines/{line_id}/more", headers={"X-CSRF-Token": token, "HX-Request": "true"}
    )
    assert r.status_code == 200 and f'id="line-{line_id}"' in r.text and 'id="week-total"' in r.text
    assert _line_qty(sessions, line_id) == before + 1
    client.post(f"/week/lines/{line_id}/less", headers={"X-CSRF-Token": token})
    assert _line_qty(sessions, line_id) == before
    with sessions() as s:
        fam = s.scalars(select(ProductFamily).where(ProductFamily.name == "Halfvolle melk")).one()
        assert fam.correction == pytest.approx(1.0)


def test_feedback_not_anymore_excludes_and_removes(
    client: TestClient, sessions: sessionmaker[Session]
) -> None:
    hid = make_household(sessions, "Thuis", "a@b.nl")
    line_id = seed_plan(sessions, hid)
    token = login(client, "a@b.nl")
    r = client.post(f"/week/lines/{line_id}/not_anymore", headers={"X-CSRF-Token": token})
    assert r.status_code == 200 and f'id="line-{line_id}"' not in r.text
    assert _line_qty(sessions, line_id) is None
    with sessions() as s:
        assert s.scalars(select(ProductFamily).where(ProductFamily.excluded)).one()


def test_feedback_enough_stock_survives_rebuild(
    client: TestClient, sessions: sessionmaker[Session]
) -> None:
    hid = make_household(sessions, "Thuis", "a@b.nl")
    line_id = seed_plan(sessions, hid)
    token = login(client, "a@b.nl")
    client.post(f"/week/lines/{line_id}/enough_stock", headers={"X-CSRF-Token": token})
    client.post("/week/rebuild", data={"csrf": token})
    with sessions() as s:
        plan = s.scalars(select(Plan)).one()
        assert plan.lines == []  # the extra stock pushes the due date past the horizon


def test_cannot_touch_other_households_line(
    client: TestClient, sessions: sessionmaker[Session]
) -> None:
    other = make_household(sessions, "Buren", "buur@b.nl")
    their_line = seed_plan(sessions, other, product_id=7000)
    make_household(sessions, "Thuis", "a@b.nl")
    token = login(client, "a@b.nl")
    r = client.post(f"/week/lines/{their_line}/not_anymore", headers={"X-CSRF-Token": token})
    assert r.status_code == 404
    assert _line_qty(sessions, their_line) is not None


def test_manual_addition_survives_rebuild(
    client: TestClient, sessions: sessionmaker[Session]
) -> None:
    hid = make_household(sessions, "Thuis", "a@b.nl")
    seed_plan(sessions, hid)
    token = login(client, "a@b.nl")
    with sessions() as s:
        knaks = s.scalars(select(ProductFamily).where(ProductFamily.name == "Knaks")).one()
    client.post(f"/week/add/{knaks.id}", data={"csrf": token})
    client.post("/week/rebuild", data={"csrf": token})
    html = client.get("/week").text
    assert "Unox Knaks" in html and "Zelf toegevoegd" in html


# --- Families ---------------------------------------------------------------------


def test_families_search_and_flags(client: TestClient, sessions: sessionmaker[Session]) -> None:
    hid = make_household(sessions, "Thuis", "a@b.nl")
    seed_plan(sessions, hid)
    token = login(client, "a@b.nl")
    r = client.get("/families?q=melk", headers={"HX-Request": "true"})
    assert "Halfvolle melk" in r.text and "Knaks" not in r.text and "<html" not in r.text
    with sessions() as s:
        knaks = s.scalars(select(ProductFamily).where(ProductFamily.name == "Knaks")).one()
    client.post(
        f"/families/{knaks.id}/flags", data={"csrf": token, "flag": "pinned", "value": "true"}
    )
    with sessions() as s:
        k = s.get(ProductFamily, knaks.id)
        assert k is not None and k.pinned


def test_family_merge_and_split(client: TestClient, sessions: sessionmaker[Session]) -> None:
    hid = make_household(sessions, "Thuis", "a@b.nl")
    seed_plan(sessions, hid)
    token = login(client, "a@b.nl")
    with sessions.begin() as s:
        s.add(
            Product(
                ah_id=1600,
                title="Campina Halfvolle melk",
                brand="Campina",
                unit_size_text="1,5 l",
                unit_amount=1500,
                unit="ml",
            )
        )
        s.flush()
        s.add(
            Purchase(
                household_id=hid,
                ah_order_id=999,
                ah_product_id=1600,
                qty=1,
                delivered_at=date.today() - timedelta(days=1),
            )
        )
        s.flush()
        assign_families(s, hid)
    with sessions() as s:
        fams = {f.name: f.id for f in s.scalars(select(ProductFamily))}
    # brand-stripped names collide, so the Campina SKU already joined the AH milk family
    milk = fams["Halfvolle melk"]
    page = client.get(f"/families/{milk}").text
    assert "Campina Halfvolle melk" in page and "AH Halfvolle melk" in page
    r = client.post(
        f"/families/{milk}/split", data={"csrf": token, "product_id": 1600}, follow_redirects=False
    )
    new_id = int(r.headers["location"].rsplit("/", 1)[1])
    client.post(f"/families/{milk}/merge", data={"csrf": token, "source_id": new_id})
    with sessions() as s:
        assert s.get(ProductFamily, new_id) is None
        assert (
            len(s.scalars(select(ProductFamily).where(ProductFamily.id == milk)).one().members) == 2
        )


def test_merge_with_different_units_is_refused(
    client: TestClient, sessions: sessionmaker[Session]
) -> None:
    hid = make_household(sessions, "Thuis", "a@b.nl")
    seed_plan(sessions, hid)
    token = login(client, "a@b.nl")
    with sessions() as s:
        fams = {f.name: f.id for f in s.scalars(select(ProductFamily))}
    r = client.post(
        f"/families/{fams['Halfvolle melk']}/merge",
        data={"csrf": token, "source_id": fams["Knaks"]},
        follow_redirects=True,
    )
    assert "kun je niet samenvoegen" in r.text


# --- Instellingen ------------------------------------------------------------------


def test_member_cannot_open_settings(client: TestClient, sessions: sessionmaker[Session]) -> None:
    make_household(sessions, "Thuis", "kind@b.nl", role="member")
    login(client, "kind@b.nl")
    r = client.get("/settings")
    assert r.status_code == 403 and "beheerder" in r.text
    assert "Instellingen" not in client.get("/week").text


def test_settings_household_pause_and_users(
    client: TestClient, sessions: sessionmaker[Session]
) -> None:
    hid = make_household(sessions, "Thuis", "a@b.nl")
    token = login(client, "a@b.nl")
    r = client.post(
        "/settings/household",
        data={"csrf": token, "cadence_days": "14", "margin_days": "1", "half_life_days": "45"},
        follow_redirects=True,
    )
    assert "Instellingen opgeslagen" in r.text
    r = client.post(
        "/settings/household",
        data={"csrf": token, "cadence_days": "99", "margin_days": "1", "half_life_days": "45"},
    )
    assert "moet tussen" in r.text
    client.post(
        "/settings/pauses", data={"csrf": token, "start": "2026-12-20", "end": "2026-12-27"}
    )
    r = client.post(
        "/settings/pauses", data={"csrf": token, "start": "2026-12-27", "end": "2026-12-20"}
    )
    assert "vóór de begindatum" in r.text
    client.post(
        "/settings/users",
        data={"csrf": token, "email": "kind@b.nl", "password": PASSWORD, "role": "member"},
    )
    with sessions() as s:
        h = s.get(Household, hid)
        assert h is not None and h.settings_json["cadence_days"] == 14
        assert len(s.scalars(select(Pause)).all()) == 1
        assert s.scalars(select(User).where(User.email == "kind@b.nl")).one().role == "member"


def test_link_ah_account_from_settings(
    client: TestClient,
    sessions: sessionmaker[Session],
    ah_api: respx.MockRouter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FERNET_KEY", Fernet.generate_key().decode())
    get_settings.cache_clear()
    make_household(sessions, "Thuis", "a@b.nl")
    token = login(client, "a@b.nl")
    route = ah_api.post("/mobile-auth/v1/auth/token").respond(
        json={"access_token": "a", "refresh_token": "r", "expires_in": 3600}
    )
    r = client.post(
        "/settings/ah",
        data={"csrf": token, "redirect_url": "appie://login-exit?code=abcdef-1234"},
        follow_redirects=True,
    )
    assert "AH-account gekoppeld" in r.text
    assert route.calls.last.request.content == b'{"clientId":"appie-ios","code":"abcdef-1234"}'
    with sessions() as s:
        assert s.scalars(select(AhAccount)).one().label == "AH"
    get_settings.cache_clear()


def test_link_ah_account_bad_code(
    client: TestClient,
    sessions: sessionmaker[Session],
    ah_api: respx.MockRouter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FERNET_KEY", Fernet.generate_key().decode())
    get_settings.cache_clear()
    make_household(sessions, "Thuis", "a@b.nl")
    token = login(client, "a@b.nl")
    ah_api.post("/mobile-auth/v1/auth/token").respond(status_code=400, json={"message": "x"})
    r = client.post(
        "/settings/ah", data={"csrf": token, "redirect_url": "appie://login-exit?code=used-code-1"}
    )
    assert "accepteerde deze code niet" in r.text
    get_settings.cache_clear()
