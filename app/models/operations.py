"""9.4 Chairs, Appointments & Queue — chairs, appointments, appointment_services, queue_entries."""

from sqlalchemy import (
    Column,
    String,
    Enum,
    Integer,
    SmallInteger,
    DateTime,
    Text,
    Index,
)

from app.models.base import Base, TimestampMixin, uuid_pk


class Chair(Base, TimestampMixin):
    """Physical chairs per branch."""

    __tablename__ = "chairs"

    id = uuid_pk()
    branch_id = Column(String, nullable=False, index=True)
    chair_number = Column(SmallInteger, nullable=False)
    assigned_staff_id = Column(String, nullable=True, index=True)
    status = Column(
        Enum("free", "occupied", "reserved", "offline", name="chair_status"),
        default="free",
        nullable=False,
    )


class Appointment(Base, TimestampMixin):
    """Bookings from all three sources (walk-in / online / staff)."""

    __tablename__ = "appointments"

    id = uuid_pk()
    business_id = Column(String, nullable=False, index=True)
    branch_id = Column(String, nullable=False, index=True)
    customer_id = Column(String, nullable=False, index=True)
    staff_id = Column(String, nullable=False, index=True)
    chair_id = Column(String, nullable=True, index=True)
    scheduled_at = Column(DateTime(timezone=True), nullable=False)
    duration_min = Column(SmallInteger, nullable=False)
    source = Column(
        Enum("walk_in", "online", "staff", name="appointment_source"),
        default="staff",
        nullable=False,
    )
    status = Column(
        Enum(
            "booked",
            "confirmed",
            "in_service",
            "completed",
            "no_show",
            "cancelled",
            name="appointment_status",
        ),
        default="booked",
        nullable=False,
    )
    notes = Column(Text, nullable=True)

    __table_args__ = (
        Index("ix_appt_branch_scheduled", "branch_id", "scheduled_at"),
        Index("ix_appt_staff_scheduled", "staff_id", "scheduled_at"),
        Index("ix_appt_customer", "customer_id"),
    )


class AppointmentService(Base, TimestampMixin):
    """Which services an appointment includes (one booking can be haircut + beard)."""

    __tablename__ = "appointment_services"

    id = uuid_pk()
    appointment_id = Column(String, nullable=False, index=True)
    service_id = Column(String, nullable=False, index=True)
    price_kes = Column(Integer, nullable=False)


class QueueEntry(Base, TimestampMixin):
    """The live walk-in queue."""

    __tablename__ = "queue_entries"

    id = uuid_pk()
    branch_id = Column(String, nullable=False, index=True)
    customer_id = Column(String, nullable=False, index=True)
    staff_id = Column(String, nullable=True, index=True)
    chair_id = Column(String, nullable=True, index=True)
    queue_number = Column(SmallInteger, nullable=False)
    status = Column(
        Enum("waiting", "serving", "served", "left", name="queue_status"),
        default="waiting",
        nullable=False,
    )
    joined_at = Column(DateTime(timezone=True), nullable=False)
    served_at = Column(DateTime(timezone=True), nullable=True)
    est_wait_min = Column(SmallInteger, nullable=True)

    __table_args__ = (
        Index("ix_queue_branch_status_num", "branch_id", "status", "queue_number"),
    )
