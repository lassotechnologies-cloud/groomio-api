"""Sale fan-out integration tests (F1) — the write that matters most.

`POST /sales` is the single most consequential operation in the product: one
clerk tap at the counter has to produce a sale, frozen commission, decremented
stock and loyalty points, atomically. Every earlier test in this suite checks the
arithmetic of one of those four. None of them checks that they happen together,
which is the property that actually protects the shop.

That is what these tests are for, and why they need a real Postgres. The models
use `JSONB`, `UUID` and native enums; a SQLite run would exercise a schema that
does not exist in production, and the whole point is to test production's schema.

Requires TEST_DATABASE_URL — see tests/conftest.py. Without it these skip.

The `user` passed to the router is the shape `get_current_user` returns, so these
call the endpoint functions directly rather than going through HTTP. That skips
routing and dependency wiring, both of which are already covered by the
route-surface tests in test_messaging.py.
"""

import uuid

import pytest
from sqlalchemy import func, select

from app.models.inventory import Product
from app.models.loyalty import LoyaltyTransaction
from app.models.money import Sale, SaleItem
from app.models.people import Customer, Staff
from app.models.tenant import Branch, Business, Service
from app.sales.router import create_sale
from app.sales.schemas import SaleCreateRequest, SaleLineRequest


# ── fixtures ────────────────────────────────────────────────────────────────
@pytest.fixture
async def shop(db):
    """A business, branch, staff and customer, all committed and re-read.

    The explicit commit matters: the router queries these back with new
    `db.scalar` calls, and unflushed state would make every lookup return None
    for reasons that have nothing to do with the code under test.
    """
    business = Business(
        owner_user_id="user_owner",
        name="Kenge Hair Studio",
        business_type="barber_shop",
        status="active",
    )
    db.add(business)
    await db.flush()

    branch = Branch(
        business_id=str(business.id),
        slug=f"kenge-{uuid.uuid4().hex[:8]}",
        name="Nairobi Branch",
    )
    db.add(branch)
    await db.flush()

    staff = Staff(
        business_id=str(business.id),
        branch_id=str(branch.id),
        user_id="user_barber",
        staff_role="barber",
        commission_type="percentage",
        commission_value=20,
        status="active",
    )
    db.add(staff)
    await db.flush()

    customer = Customer(
        business_id=str(business.id),
        branch_id=str(branch.id),
        full_name="Wanjiku",
        phone="+254722000111",
        loyalty_points=0,
    )
    db.add(customer)
    await db.commit()

    # Re-read so the objects carry server defaults and are attached cleanly.
    for model in (business, branch, staff, customer):
        await db.refresh(model)

    return {
        "business": business,
        "branch": branch,
        "staff": staff,
        "customer": customer,
        "user": {
            "user_id": "user_owner",
            "role": "owner",
            "business_id": str(business.id),
        },
    }


@pytest.fixture
async def catalogue(db, shop):
    """One service and one product, committed."""
    service = Service(
        branch_id=str(shop["branch"].id),
        name="Cut and shave",
        price_kes=1000,
        # `services.duration_min` is NOT NULL in the schema (migration 0002):
        # a service with no length cannot be scheduled or sold, so the fixture
        # supplies the 45 minutes a cut-and-shave actually takes.
        duration_min=45,
        is_active=True,
    )
    product = Product(
        branch_id=str(shop["branch"].id),
        name="Pomade",
        cost_price_kes=300,
        sell_price_kes=500,
        stock_qty=10,
        low_stock_threshold=2,
    )
    db.add_all([service, product])
    await db.commit()
    await db.refresh(service)
    await db.refresh(product)
    return {"service": service, "product": product}


def _payload(shop, catalogue, **overrides) -> SaleCreateRequest:
    body = {
        "branch_id": str(shop["branch"].id),
        "staff_id": str(shop["staff"].id),
        "customer_id": str(shop["customer"].id),
        "payment_method": "mpesa",
        "lines": [
            {
                "item_type": "service",
                "service_id": str(catalogue["service"].id),
                "quantity": 1,
            }
        ],
    }
    body.update(overrides)
    return SaleCreateRequest(**body)


# ── the four effects ────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_a_sale_records_the_sale_and_its_line(db, shop, catalogue):
    payload = _payload(shop, catalogue)
    result = await create_sale(payload, db, shop["user"])
    await db.commit()

    sale = await db.scalar(select(Sale).where(Sale.id == result.sale.id))
    assert sale is not None
    assert sale.total_kes == 1000

    items = list(
        await db.scalars(select(SaleItem).where(SaleItem.sale_id == result.sale.id))
    )
    assert len(items) == 1
    assert items[0].price_kes == 1000


@pytest.mark.asyncio
async def test_commission_is_frozen_onto_the_line(db, shop, catalogue):
    """20% of 1000. Written to the row, not recomputed at payroll time.

    If this were derived later, a barber's rate change would silently rewrite
    what they earned in January.
    """
    result = await create_sale(_payload(shop, catalogue), db, shop["user"])
    await db.commit()

    assert result.commission_kes == 200

    item = await db.scalar(select(SaleItem).where(SaleItem.sale_id == result.sale.id))
    assert item.commission_kes == 200


@pytest.mark.asyncio
async def test_stock_is_decremented_and_reported(db, shop, catalogue):
    payload = _payload(
        shop,
        catalogue,
        lines=[
            {
                "item_type": "product",
                "product_id": str(catalogue["product"].id),
                "quantity": 3,
            }
        ],
    )
    result = await create_sale(payload, db, shop["user"])
    await db.commit()

    assert "Pomade" in result.products_decremented
    await db.refresh(catalogue["product"])
    assert catalogue["product"].stock_qty == 7  # was 10


@pytest.mark.asyncio
async def test_loyalty_points_are_awarded_and_recorded(db, shop, catalogue):
    """1 point per whole KES 100. The transaction row is the audit trail."""
    result = await create_sale(_payload(shop, catalogue), db, shop["user"])
    await db.commit()

    assert result.loyalty_points_awarded == 10

    await db.refresh(shop["customer"])
    assert shop["customer"].loyalty_points == 10

    rows = list(
        await db.scalars(
            select(LoyaltyTransaction).where(
                LoyaltyTransaction.customer_id == str(shop["customer"].id)
            )
        )
    )
    assert len(rows) == 1
    assert rows[0].points == 10


@pytest.mark.asyncio
async def test_a_sale_without_a_customer_earns_no_points(db, shop, catalogue):
    result = await create_sale(
        _payload(shop, catalogue, customer_id=None), db, shop["user"]
    )
    await db.commit()

    assert result.loyalty_points_awarded == 0


# ── pricing is server-authoritative ─────────────────────────────────────────
@pytest.mark.asyncio
async def test_a_mismatched_expected_total_is_refused(db, shop, catalogue):
    """A mis-keyed amount is caught, not silently recorded."""
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        await create_sale(
            _payload(shop, catalogue, expected_total_kes=1), db, shop["user"]
        )

    assert exc.value.status_code == 409

    count = await db.scalar(select(func.count()).select_from(Sale))
    assert count == 0, "a refused sale must not leave a row behind"


@pytest.mark.asyncio
async def test_the_client_cannot_supply_a_price(db, shop, catalogue):
    """`SaleLineRequest` has no price field, so the client cannot offer one.

    This is the defence against under-reporting a sale to dodge commission.

    Pydantic ignores unknown keys by default, so the guarantee is structural
    rather than an exception: there is no `price_kes` field for a client value
    to land in, and an instance built from a payload that carries one exposes
    no price at all.
    """
    assert "price_kes" not in SaleLineRequest.model_fields

    line = SaleLineRequest(
        item_type="service",
        service_id=str(catalogue["service"].id),
        quantity=1,
        price_kes=1,
    )
    assert not hasattr(line, "price_kes")
    assert "price_kes" not in (line.model_extra or {})


# ── atomicity: the property the unit tests cannot reach ─────────────────────
@pytest.mark.asyncio
async def test_overselling_is_refused_and_writes_nothing(db, shop, catalogue):
    """The whole fan-out must not land partially when one line is invalid.

    A sale with a valid service and an impossible product quantity is rejected
    before anything is written — no sale, no commission, no stock movement, no
    points. This is the test that would have caught a partial commit.
    """
    from fastapi import HTTPException

    payload = _payload(
        shop,
        catalogue,
        lines=[
            {
                "item_type": "service",
                "service_id": str(catalogue["service"].id),
                "quantity": 1,
            },
            {
                "item_type": "product",
                "product_id": str(catalogue["product"].id),
                "quantity": 99,
            },
        ],
    )

    with pytest.raises(HTTPException) as exc:
        await create_sale(payload, db, shop["user"])
    assert exc.value.status_code == 409

    assert await db.scalar(select(func.count()).select_from(Sale)) == 0
    assert await db.scalar(select(func.count()).select_from(SaleItem)) == 0
    assert await db.scalar(select(func.count()).select_from(LoyaltyTransaction)) == 0

    await db.refresh(catalogue["product"])
    assert catalogue["product"].stock_qty == 10, "stock must be untouched"
    await db.refresh(shop["customer"])
    assert shop["customer"].loyalty_points == 0


@pytest.mark.asyncio
async def test_a_suspended_staff_member_cannot_sell(db, shop, catalogue):
    """Suspension must stop the sale, not just hide the barber from a roster."""
    from fastapi import HTTPException

    shop["staff"].status = "suspended"
    await db.commit()

    with pytest.raises(HTTPException) as exc:
        await create_sale(_payload(shop, catalogue), db, shop["user"])
    assert exc.value.status_code == 404

    assert await db.scalar(select(func.count()).select_from(Sale)) == 0


# ── tenancy ─────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_a_branch_from_another_business_is_404(db, shop, catalogue):
    """Cross-tenant access returns 404, never 403 (Doc 4.3).

    A 403 would confirm the branch exists, which is itself a leak.
    """
    from fastapi import HTTPException

    rival = Business(
        owner_user_id="user_rival",
        name="Rival Salon",
        business_type="salon",
        status="active",
    )
    db.add(rival)
    await db.flush()
    rival_branch = Branch(
        business_id=str(rival.id),
        slug=f"rival-{uuid.uuid4().hex[:8]}",
        name="Their Branch",
    )
    db.add(rival_branch)
    await db.commit()

    with pytest.raises(HTTPException) as exc:
        await create_sale(
            _payload(shop, catalogue, branch_id=str(rival_branch.id)),
            db,
            shop["user"],
        )

    assert exc.value.status_code == 404
    assert exc.value.status_code != 403


@pytest.mark.asyncio
async def test_a_service_from_another_branch_is_refused(db, shop, catalogue):
    """A chain's branches stock and price independently (Doc 10.7)."""
    from fastapi import HTTPException

    other = Business(
        owner_user_id="user_other",
        name="Mombasa Outlet",
        business_type="barber_shop",
        status="active",
    )
    db.add(other)
    await db.flush()
    other_branch = Branch(
        business_id=str(other.id),
        slug=f"mombasa-{uuid.uuid4().hex[:8]}",
        name="Mombasa",
    )
    db.add(other_branch)
    await db.flush()
    other_service = Service(
        branch_id=str(other_branch.id),
        name="Their cut",
        price_kes=999,
        duration_min=30,
        is_active=True,
    )
    db.add(other_service)
    await db.commit()

    with pytest.raises(HTTPException) as exc:
        await create_sale(
            _payload(
                shop,
                {"service": other_service, "product": catalogue["product"]},
            ),
            db,
            shop["user"],
        )

    assert exc.value.status_code in (400, 404)
    assert await db.scalar(select(func.count()).select_from(Sale)) == 0


@pytest.mark.asyncio
async def test_an_inactive_service_cannot_be_sold(db, shop, catalogue):
    """A retired service must not be sellable, or it keeps earning commission."""
    from fastapi import HTTPException

    catalogue["service"].is_active = False
    await db.commit()

    with pytest.raises(HTTPException):
        await create_sale(_payload(shop, catalogue), db, shop["user"])


# ── low stock ───────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_low_stock_is_reported_when_the_threshold_is_crossed(db, shop, catalogue):
    """A product dropping to its threshold is what prompts a restock."""
    payload = _payload(
        shop,
        catalogue,
        lines=[
            {
                "item_type": "product",
                "product_id": str(catalogue["product"].id),
                "quantity": 8,
            }
        ],
    )
    result = await create_sale(payload, db, shop["user"])
    await db.commit()

    # 10 - 8 = 2, and the threshold is 2.
    assert result.low_stock_alerts
    assert "2 left" in result.low_stock_alerts[0]
