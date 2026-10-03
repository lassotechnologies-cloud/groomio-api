"""Sale routes — API group F1, F2 (Doc 10.7).

F1 is the most consequential write in the product. One call a clerk makes at the
counter has to fan out into four effects (Doc 10.7):

  1. the sale itself, priced from the catalogue — never from the client
  2. the barber's commission, frozen onto each line at sale time
  3. stock decremented for every product sold
  4. loyalty points earned by the customer

All four run in the request's single transaction. A partial application would be
worse than a failure: a customer charged but no commission recorded is a payroll
dispute, and a customer charged but no points is a visible bug. So the whole
thing either lands or none of it does.

Commission is written onto `sale_items` rather than recomputed later, so a barber's
rate change from March does not silently rewrite their January earnings.
"""

from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.deps import CurrentUser, require_role
from app.core.security import now_utc
from app.core.tenancy import resolve_business_id
from app.models.inventory import Product
from app.models.loyalty import LoyaltyTransaction
from app.models.money import Sale, SaleItem
from app.models.people import Customer, Staff
from app.models.tenant import Branch, Service
from app.sales.schemas import (
    SaleCreateRequest,
    SaleFanoutResponse,
    SaleLineResponse,
    SaleResponse,
)

router = APIRouter(tags=["sales"])

Db = Annotated[AsyncSession, Depends(get_db)]
ClerkOrOwner = Depends(require_role("owner", "clerk"))

# Loyalty: 1 point per whole KES 100 spent. Simple enough that a customer can
# predict it, which matters more here than a sophisticated scheme (CL5).
POINTS_PER_100_KES = 1


async def _load_branch(db: AsyncSession, business_id: str, branch_id: str) -> Branch:
    branch = await db.scalar(
        select(Branch).where(Branch.id == branch_id, Branch.business_id == business_id)
    )
    if not branch:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Branch not found"
        )
    return branch


def _commission_kes(staff: Staff, line_total: int) -> int:
    """Barber's cut of one line. Rounded down — never pay out a fraction of a shilling."""
    if staff.commission_type == "percentage":
        return (line_total * staff.commission_value) // 100
    # Fixed: the same amount per line regardless of price, times quantity.
    return staff.commission_value


# ── F1 — record a sale ───────────────────────────────────────────────────────
@router.post(
    "/sales",
    response_model=SaleFanoutResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[ClerkOrOwner],
)
async def create_sale(
    payload: SaleCreateRequest, db: Db, user: CurrentUser
) -> SaleFanoutResponse:
    """CL4 — record services + products, take payment, and settle every side effect."""
    business_id = await resolve_business_id(db, user)
    await _load_branch(db, business_id, payload.branch_id)

    staff = await db.scalar(
        select(Staff).where(
            Staff.id == payload.staff_id,
            Staff.business_id == business_id,
            Staff.status == "active",
        )
    )
    if not staff:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Staff not found or suspended"
        )

    customer = None
    if payload.customer_id:
        customer = await db.scalar(
            select(Customer).where(
                Customer.id == payload.customer_id, Customer.business_id == business_id
            )
        )
        if not customer:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Customer not found"
            )

    # ── price every line from the catalogue ──────────────────────────────────
    prepared: list[dict] = []
    total = 0
    for line in payload.lines:
        if line.item_type == "service":
            svc = await db.scalar(
                select(Service).where(
                    Service.id == line.service_id,
                    Service.branch_id == payload.branch_id,
                    Service.is_active.is_(True),
                )
            )
            if not svc:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Service {line.service_id} not in this branch",
                )
            line_total = svc.price_kes * line.quantity
            prepared.append(
                {
                    "item_type": "service",
                    "service_id": svc.id,
                    "product_id": None,
                    "name": svc.name,
                    "price_kes": svc.price_kes,
                    "quantity": line.quantity,
                    "commission_kes": _commission_kes(staff, line_total),
                }
            )
        else:
            product = await db.scalar(
                select(Product).where(
                    Product.id == line.product_id,
                    Product.branch_id == payload.branch_id,
                )
            )
            if not product:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Product {line.product_id} not in this branch",
                )
            # Refuse to oversell: stock is physical, and a negative count is a lie
            # the shop floor would have to reconcile by hand.
            if product.stock_qty < line.quantity:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=(
                        f"{product.name}: only {product.stock_qty} left, "
                        f"tried to sell {line.quantity}"
                    ),
                )
            line_total = product.sell_price_kes * line.quantity
            prepared.append(
                {
                    "item_type": "product",
                    "service_id": None,
                    "product_id": product.id,
                    "name": product.name,
                    "price_kes": product.sell_price_kes,
                    "quantity": line.quantity,
                    # Products carry no commission — the barber is paid for time,
                    # and paying on retail goods would double-dip the shop's margin.
                    "commission_kes": 0,
                }
            )
        total += line_total

    if payload.expected_total_kes is not None and payload.expected_total_kes != total:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Total mismatch: expected {payload.expected_total_kes}, actual {total}",
        )

    # ── the sale ─────────────────────────────────────────────────────────────
    # Every id column on these tables is VARCHAR, but `Model.id` is a UUID, so
    # the value is stringified on the way in. asyncpg rejects a `uuid.UUID`
    # bound to a VARCHAR parameter ("expected str, got UUID"), which fails the
    # whole fan-out at flush time — the same reason `str(sale.id)` is used for
    # `sale_items.sale_id` below.
    sale = Sale(
        business_id=business_id,
        branch_id=payload.branch_id,
        customer_id=str(customer.id) if customer else None,
        staff_id=str(staff.id),
        total_kes=total,
        payment_method=payload.payment_method,
        created_by=user["user_id"],
    )
    db.add(sale)
    await db.flush()

    # Keep the ORM objects: after the flush below they carry real primary keys,
    # so the response echoes ids the client can use in a refund.
    created_items: list[SaleItem] = []
    for item in prepared:
        row = SaleItem(
            sale_id=str(sale.id),
            item_type=item["item_type"],
            # VARCHAR columns; see the note on the Sale insert above.
            service_id=str(item["service_id"]) if item["service_id"] else None,
            product_id=str(item["product_id"]) if item["product_id"] else None,
            name=item["name"],
            price_kes=item["price_kes"],
            quantity=item["quantity"],
            commission_kes=item["commission_kes"],
        )
        db.add(row)
        created_items.append(row)

    # ── stock decrement + low-stock alerts ───────────────────────────────────
    decremented: list[str] = []
    low_stock: list[str] = []
    for item in prepared:
        if item["item_type"] != "product":
            continue
        product = await db.scalar(
            select(Product).where(Product.id == item["product_id"])
        )
        product.stock_qty -= item["quantity"]
        decremented.append(item["name"])
        if product.stock_qty <= product.low_stock_threshold:
            low_stock.append(f"{product.name} ({product.stock_qty} left)")

    # ── loyalty points (CL4: earned automatically) ───────────────────────────
    points = (total // 100) * POINTS_PER_100_KES if customer else 0
    if customer and points > 0:
        customer.loyalty_points = (customer.loyalty_points or 0) + points
        db.add(
            LoyaltyTransaction(
                customer_id=str(customer.id),
                points=points,
                reason="visit",
                sale_id=str(sale.id),
            )
        )

    await db.flush()

    sale_out = SaleResponse(
        id=str(sale.id),
        business_id=sale.business_id,
        branch_id=sale.branch_id,
        customer_id=sale.customer_id,
        staff_id=sale.staff_id,
        total_kes=total,
        payment_method=sale.payment_method,
        created_at=sale.created_at or now_utc(),
        lines=[
            SaleLineResponse(
                id=str(row.id),
                item_type=row.item_type,
                name=row.name,
                price_kes=row.price_kes,
                quantity=row.quantity,
                commission_kes=row.commission_kes,
                service_id=row.service_id,
                product_id=row.product_id,
            )
            for row in created_items
        ],
    )

    return SaleFanoutResponse(
        sale=sale_out,
        commission_kes=sum(i["commission_kes"] for i in prepared),
        loyalty_points_awarded=points,
        products_decremented=decremented,
        low_stock_alerts=low_stock,
    )


# ── F2 — sales list ──────────────────────────────────────────────────────────
@router.get("/sales", response_model=list[SaleResponse], dependencies=[ClerkOrOwner])
async def list_sales(
    db: Db,
    user: CurrentUser,
    on_date: Optional[str] = Query(None, alias="date", description="YYYY-MM-DD"),
    branch_id: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=200),
) -> list[SaleResponse]:
    """Sales for a day, newest first. Scoped to the caller's business."""
    from datetime import date as _date, datetime, timedelta, timezone

    business_id = await resolve_business_id(db, user)

    if on_date:
        try:
            day = datetime.strptime(on_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="date must be YYYY-MM-DD",
            ) from None
        start, end = day, day + timedelta(days=1)
    else:
        start = now_utc() - timedelta(days=1)
        end = now_utc() + timedelta(minutes=1)

    stmt = select(Sale).where(
        Sale.business_id == business_id,
        Sale.created_at >= start,
        Sale.created_at < end,
    )
    if branch_id:
        stmt = stmt.where(Sale.branch_id == branch_id)

    sales = list(await db.scalars(stmt.order_by(Sale.created_at.desc()).limit(limit)))
    if not sales:
        return []

    # Batch the line lookup rather than a query per sale.
    sale_ids = [str(s.id) for s in sales]
    items: dict[str, list[SaleItem]] = {i: [] for i in sale_ids}
    for item in await db.scalars(
        select(SaleItem).where(SaleItem.sale_id.in_(sale_ids))
    ):
        items.setdefault(item.sale_id, []).append(item)

    return [
        SaleResponse(
            id=str(s.id),
            business_id=s.business_id,
            branch_id=s.branch_id,
            customer_id=s.customer_id,
            staff_id=s.staff_id,
            total_kes=s.total_kes,
            payment_method=s.payment_method,
            created_at=s.created_at,
            lines=[
                SaleLineResponse(
                    id=str(i.id),
                    item_type=i.item_type,
                    name=i.name,
                    price_kes=i.price_kes,
                    quantity=i.quantity,
                    commission_kes=i.commission_kes,
                    service_id=i.service_id,
                    product_id=i.product_id,
                )
                for i in items.get(str(s.id), [])
            ],
        )
        for s in sales
    ]
