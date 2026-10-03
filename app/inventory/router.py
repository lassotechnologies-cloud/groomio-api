"""Inventory routes — API group G1, G2 (Doc 10.7).

Products are branch-scoped, not business-scoped: two branches of the same chain
stock different shelves, and a Nairobi salon running an outlet in Mombasa cannot
sell what the other outlet never received. Every product query therefore goes
through a branch that has been proven to belong to the caller's business.

Stock is deliberately mutable without an immutable ledger behind it. A full
`stock_movements` table belongs in a later phase; until then the shop floor needs
a restock button that works today, and a balance that is at least honest.
"""

from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.deps import CurrentUser, require_role
from app.core.tenancy import resolve_business_id
from app.inventory.schemas import (
    ProductCreateRequest,
    ProductResponse,
    ProductUpdateRequest,
    StockAdjustRequest,
    SupplierCreateRequest,
    SupplierResponse,
    SupplierUpdateRequest,
)
from app.models.inventory import Product, Supplier
from app.models.tenant import Branch

router = APIRouter(tags=["inventory"])

Db = Annotated[AsyncSession, Depends(get_db)]
ClerkOrOwner = Depends(require_role("owner", "clerk"))


# ── helpers ────────────────────────────────────────────────────────────────
async def _branch(db: AsyncSession, business_id: str, branch_id: str) -> Branch:
    branch = await db.scalar(
        select(Branch).where(Branch.id == branch_id, Branch.business_id == business_id)
    )
    if not branch:
        # 404, not 403: another shop's branch should be indistinguishable from
        # one that does not exist (Doc 4.3).
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Branch not found"
        )
    return branch


def _out(p: Product) -> ProductResponse:
    return ProductResponse(
        id=str(p.id),
        branch_id=p.branch_id,
        name=p.name,
        category=p.category,
        cost_price_kes=p.cost_price_kes,
        sell_price_kes=p.sell_price_kes,
        stock_qty=p.stock_qty,
        low_stock_threshold=p.low_stock_threshold,
        is_low_stock=p.stock_qty <= p.low_stock_threshold,
        margin_kes=p.sell_price_kes - p.cost_price_kes,
        created_at=p.created_at,
    )


# ── G1 — products ──────────────────────────────────────────────────────────
@router.get(
    "/branches/{branch_id}/products",
    response_model=list[ProductResponse],
    dependencies=[ClerkOrOwner],
)
async def list_products(
    branch_id: str,
    db: Db,
    user: CurrentUser,
    q: Optional[str] = Query(None, description="Name or category search"),
    low_stock: bool = Query(False, description="Only items at or below threshold"),
    category: Optional[str] = Query(None),
) -> list[ProductResponse]:
    """G1 — the branch's shelf. Defaults to everything, so a new branch is never blank."""
    business_id = await resolve_business_id(db, user)
    await _branch(db, business_id, branch_id)

    stmt = select(Product).where(Product.branch_id == branch_id)
    if category:
        stmt = stmt.where(Product.category == category)
    if q:
        like = f"%{q}%"
        stmt = stmt.where(or_(Product.name.ilike(like), Product.category.ilike(like)))
    if low_stock:
        # Compared in Python-free SQL so the filter uses the index on stock_qty.
        stmt = stmt.where(Product.stock_qty <= Product.low_stock_threshold)

    return [_out(p) for p in await db.scalars(stmt.order_by(Product.name))]


@router.post(
    "/branches/{branch_id}/products",
    response_model=ProductResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[ClerkOrOwner],
)
async def create_product(
    branch_id: str, payload: ProductCreateRequest, db: Db, user: CurrentUser
) -> ProductResponse:
    """G1 — add a product and its opening stock in one call."""
    business_id = await resolve_business_id(db, user)
    await _branch(db, business_id, branch_id)

    product = Product(
        branch_id=branch_id,
        name=payload.name,
        category=payload.category,
        cost_price_kes=payload.cost_price_kes,
        sell_price_kes=payload.sell_price_kes,
        stock_qty=payload.stock_qty,
        low_stock_threshold=payload.low_stock_threshold,
    )
    db.add(product)
    await db.flush()
    return _out(product)


@router.patch(
    "/branches/{branch_id}/products/{product_id}",
    response_model=ProductResponse,
    dependencies=[ClerkOrOwner],
)
async def update_product(
    branch_id: str,
    product_id: str,
    payload: ProductUpdateRequest,
    db: Db,
    user: CurrentUser,
) -> ProductResponse:
    """G1 — edit details. `stock_qty` is not editable here on purpose: use the
    stock endpoint, so every count change is attributable to a reason."""
    business_id = await resolve_business_id(db, user)
    await _branch(db, business_id, branch_id)

    product = await db.scalar(
        select(Product).where(Product.id == product_id, Product.branch_id == branch_id)
    )
    if not product:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Product not found"
        )

    for field in (
        "name",
        "category",
        "cost_price_kes",
        "sell_price_kes",
        "low_stock_threshold",
    ):
        value = getattr(payload, field)
        if value is not None:
            setattr(product, field, value)

    await db.flush()
    return _out(product)


@router.post(
    "/branches/{branch_id}/products/{product_id}/stock",
    response_model=ProductResponse,
    dependencies=[ClerkOrOwner],
)
async def adjust_stock(
    branch_id: str,
    product_id: str,
    payload: StockAdjustRequest,
    db: Db,
    user: CurrentUser,
) -> ProductResponse:
    """G1 — restock, breakage, or a recount correction."""
    business_id = await resolve_business_id(db, user)
    await _branch(db, business_id, branch_id)

    product = await db.scalar(
        select(Product).where(Product.id == product_id, Product.branch_id == branch_id)
    )
    if not product:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Product not found"
        )

    new_qty = product.stock_qty + payload.delta
    if new_qty < 0:
        # Same reason as the oversell guard in POST /sales: a negative count on a
        # physical shelf is a claim the shop cannot back.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Cannot remove {-payload.delta}; only {product.stock_qty} in stock",
        )
    product.stock_qty = new_qty
    await db.flush()
    return _out(product)


@router.delete(
    "/branches/{branch_id}/products/{product_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[ClerkOrOwner],
)
async def delete_product(
    branch_id: str, product_id: str, db: Db, user: CurrentUser
) -> None:
    """G1 — remove a product entirely. Deleting a product that appears in past sales
    leaves those receipts with a dangling reference, so this is reserved for items
    that were catalogued by mistake; the rest should be set to zero stock instead."""
    business_id = await resolve_business_id(db, user)
    await _branch(db, business_id, branch_id)

    product = await db.scalar(
        select(Product).where(Product.id == product_id, Product.branch_id == branch_id)
    )
    if not product:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Product not found"
        )
    await db.delete(product)


# ── G2 — suppliers ─────────────────────────────────────────────────────────
def _supplier_out(s: Supplier) -> SupplierResponse:
    return SupplierResponse(
        id=str(s.id),
        business_id=s.business_id,
        name=s.name,
        phone=s.phone,
        last_order_at=s.last_order_at,
    )


@router.get("/business/suppliers", response_model=list[SupplierResponse])
async def list_suppliers(
    db: Db, user: CurrentUser, _role: str = Depends(require_role("owner"))
) -> list[SupplierResponse]:
    """G2 — owner-only: supplier pricing is commercially sensitive to staff."""
    business_id = await resolve_business_id(db, user)
    rows = await db.scalars(
        select(Supplier)
        .where(Supplier.business_id == business_id)
        .order_by(Supplier.name)
    )
    return [_supplier_out(s) for s in rows]


@router.post(
    "/business/suppliers",
    response_model=SupplierResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_role("owner"))],
)
async def create_supplier(
    payload: SupplierCreateRequest, db: Db, user: CurrentUser
) -> SupplierResponse:
    """G2 — a shop buys from a person or shop; both are just a name and a phone."""
    business_id = await resolve_business_id(db, user)
    supplier = Supplier(business_id=business_id, name=payload.name, phone=payload.phone)
    db.add(supplier)
    await db.flush()
    return _supplier_out(supplier)


@router.patch(
    "/business/suppliers/{supplier_id}",
    response_model=SupplierResponse,
    dependencies=[Depends(require_role("owner"))],
)
async def update_supplier(
    supplier_id: str, payload: SupplierUpdateRequest, db: Db, user: CurrentUser
) -> SupplierResponse:
    """G2 — correct a name or number."""
    business_id = await resolve_business_id(db, user)
    supplier = await db.scalar(
        select(Supplier).where(
            Supplier.id == supplier_id, Supplier.business_id == business_id
        )
    )
    if not supplier:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Supplier not found"
        )

    for field in ("name", "phone"):
        value = getattr(payload, field)
        if value is not None:
            setattr(supplier, field, value)

    await db.flush()
    return _supplier_out(supplier)
