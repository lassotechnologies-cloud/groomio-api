"""9.3 People: Customers & Staff — customers, staff, schedules, attendance, leave."""

from sqlalchemy import Column, String, Enum, Integer, Date, DateTime, Time, Text, Index

from app.models.base import Base, TimestampMixin, uuid_pk


class Customer(Base, TimestampMixin):
    """Shop customers (NOT app logins — they use the booking portal by phone link)."""

    __tablename__ = "customers"

    id = uuid_pk()
    business_id = Column(String, nullable=False, index=True)
    branch_id = Column(String, nullable=False, index=True)
    full_name = Column(String(120), nullable=False)
    phone = Column(String(15), nullable=False)
    photo_url = Column(String, nullable=True)
    preferences = Column(Text, nullable=True)
    notes = Column(Text, nullable=True)
    loyalty_points = Column(Integer, default=0, nullable=False)
    birthday = Column(Date, nullable=True)

    __table_args__ = (
        Index("ix_customers_biz_phone", "business_id", "phone"),
        Index("ix_customers_biz_name", "business_id", "full_name"),
    )


class Staff(Base, TimestampMixin):
    """Barbers/clerks as employees of a business. A person = 1 users + 1 staff row."""

    __tablename__ = "staff"

    id = uuid_pk()
    business_id = Column(String, nullable=False, index=True)
    branch_id = Column(String, nullable=False, index=True)
    user_id = Column(String, nullable=False, index=True)
    staff_role = Column(Enum("barber", "clerk", name="staff_role"), nullable=False)
    commission_type = Column(
        Enum("percentage", "fixed", name="commission_type"), nullable=False
    )
    commission_value = Column(Integer, nullable=False)
    photo_url = Column(String, nullable=True)
    status = Column(
        Enum("active", "suspended", name="staff_status"),
        default="active",
        nullable=False,
    )


class Schedule(Base):
    """Weekly working pattern per staff member."""

    __tablename__ = "schedules"

    id = uuid_pk()
    staff_id = Column(String, nullable=False, index=True)
    day_of_week = Column(Integer, nullable=False)  # 0=Mon … 6=Sun
    start_time = Column(Time, nullable=False)
    end_time = Column(Time, nullable=False)


class Attendance(Base):
    """Check-in/out records."""

    __tablename__ = "attendance"

    id = uuid_pk()
    staff_id = Column(String, nullable=False, index=True)
    branch_id = Column(String, nullable=False, index=True)
    work_date = Column(Date, nullable=False)
    check_in_at = Column(DateTime(timezone=True), nullable=True)
    check_out_at = Column(DateTime(timezone=True), nullable=True)


class LeaveRequest(Base, TimestampMixin):
    """Barber asks, owner approves."""

    __tablename__ = "leave_requests"

    id = uuid_pk()
    staff_id = Column(String, nullable=False, index=True)
    leave_type = Column(
        Enum("annual", "sick", "other", name="leave_type"), nullable=False
    )
    start_date = Column(Date, nullable=False)
    end_date = Column(Date, nullable=False)
    reason = Column(Text, nullable=True)
    status = Column(
        Enum("pending", "approved", "rejected", name="leave_status"),
        default="pending",
        nullable=False,
    )
    decided_by = Column(String, nullable=True, index=True)
