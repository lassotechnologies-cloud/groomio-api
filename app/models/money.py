"""9.5 Money: Sales, Expenses, Commissions — sales, sale_items, expenses, commissions."""

from sqlalchemy import Column, String, Enum, Integer, Date, Index

from app.models.base import Base, TimestampMixin, uuid_pk


class Sale(Base, TimestampMixin):
    """One row per completed transaction (services + products)."""

    __tablename__ = "sales"

    id = uuid_pk()
    business_id = Column(String, nullable=False, index=True)
    branch_id = Column(String, nullable=False, index=True)
    customer_id = Column(String, nullable=True, index=True)
    staff_id = Column(String, nullable=False, index=True)
    total_kes = Column(Integer, nullable=False)
    payment_method = Column(
        Enum("cash", "mpesa", "pesapal", name="payment_method"),
        default="cash",
        nullable=False,
    )
    created_by = Column(String, nullable=False, index=True)

    __table_args__ = (Index("ix_sales_biz_paid", "business_id", "created_at"),)


class SaleItem(Base, TimestampMixin):
    """Line items of a sale; also the commission record."""

    __tablename__ = "sale_items"

    id = uuid_pk()
    sale_id = Column(String, nullable=False, index=True)
    item_type = Column(Enum("service", "product", name="item_type"), nullable=False)
    service_id = Column(String, nullable=True, index=True)
    product_id = Column(String, nullable=True, index=True)
    name = Column(String(120), nullable=False)
    price_kes = Column(Integer, nullable=False)
    quantity = Column(Integer, default=1, nullable=False)
    commission_kes = Column(Integer, default=0, nullable=False)


class Expense(Base, TimestampMixin):
    """Money out (rent, stock purchase, utilities)."""

    __tablename__ = "expenses"

    id = uuid_pk()
    branch_id = Column(String, nullable=False, index=True)
    category = Column(
        Enum(
            "rent", "stock", "utilities", "salaries", "other", name="expense_category"
        ),
        nullable=False,
    )
    amount_kes = Column(Integer, nullable=False)
    note = Column(String, nullable=True)
    incurred_at = Column(Date, nullable=False)
    recorded_by = Column(String, nullable=False, index=True)


class Commission(Base, TimestampMixin):
    """Monthly summary per barber (computed from sale_items; kept for quick reads)."""

    __tablename__ = "commissions"

    id = uuid_pk()
    staff_id = Column(String, nullable=False, index=True)
    branch_id = Column(String, nullable=False, index=True)
    period_start = Column(Date, nullable=False)
    period_end = Column(Date, nullable=False)
    earned_kes = Column(Integer, default=0, nullable=False)
    bonus_kes = Column(Integer, default=0, nullable=False)
    status = Column(
        Enum("open", "paid", name="commission_status"), default="open", nullable=False
    )
