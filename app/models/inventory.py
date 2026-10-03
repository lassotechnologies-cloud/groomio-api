"""9.6 Inventory — products, suppliers."""

from sqlalchemy import Column, String, Integer, Date

from app.models.base import Base, TimestampMixin, uuid_pk


class Product(Base, TimestampMixin):
    """Retail stock."""

    __tablename__ = "products"

    id = uuid_pk()
    branch_id = Column(String, nullable=False, index=True)
    name = Column(String(120), nullable=False)
    category = Column(String(80), nullable=True)
    cost_price_kes = Column(Integer, default=0, nullable=False)
    sell_price_kes = Column(Integer, nullable=False)
    stock_qty = Column(Integer, default=0, nullable=False)
    low_stock_threshold = Column(Integer, default=5, nullable=False)


class Supplier(Base, TimestampMixin):
    """Who the shop buys from."""

    __tablename__ = "suppliers"

    id = uuid_pk()
    business_id = Column(String, nullable=False, index=True)
    name = Column(String(120), nullable=False)
    phone = Column(String(15), nullable=True)
    last_order_at = Column(Date, nullable=True)
