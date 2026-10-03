"""operations tables — customers, staff, schedules, attendance, leave_requests,
chairs, appointments, appointment_services, queue_entries

Mirrors app/models/people.py and app/models/operations.py.

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-28
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

# revision identifiers, used by Alembic.
revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def _ts() -> list:
    return [
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    ]


def _pk() -> sa.Column:
    return sa.Column(
        "id", UUID, primary_key=True, server_default=sa.func.gen_random_uuid()
    )


def upgrade() -> None:
    # ── customers — shop customers, not app logins ──────────────────────────
    op.create_table(
        "customers",
        _pk(),
        sa.Column("business_id", sa.String, nullable=False),
        sa.Column("branch_id", sa.String, nullable=False),
        sa.Column("full_name", sa.String(120), nullable=False),
        sa.Column("phone", sa.String(15), nullable=False),
        sa.Column("photo_url", sa.String, nullable=True),
        sa.Column("preferences", sa.Text, nullable=True),
        sa.Column("notes", sa.Text, nullable=True),
        sa.Column("loyalty_points", sa.Integer, server_default="0", nullable=False),
        sa.Column("birthday", sa.Date, nullable=True),
        *_ts(),
    )
    op.create_index("ix_customers_business_id", "customers", ["business_id"])
    op.create_index("ix_customers_branch_id", "customers", ["branch_id"])
    op.create_index("ix_customers_biz_phone", "customers", ["business_id", "phone"])
    op.create_index("ix_customers_biz_name", "customers", ["business_id", "full_name"])

    # ── staff — barbers/clerks as employees ─────────────────────────────────
    op.create_table(
        "staff",
        _pk(),
        sa.Column("business_id", sa.String, nullable=False),
        sa.Column("branch_id", sa.String, nullable=False),
        sa.Column("user_id", sa.String, nullable=False),
        sa.Column(
            "staff_role",
            sa.Enum("barber", "clerk", name="staff_role"),
            nullable=False,
        ),
        sa.Column(
            "commission_type",
            sa.Enum("percentage", "fixed", name="commission_type"),
            nullable=False,
        ),
        sa.Column("commission_value", sa.Integer, nullable=False),
        sa.Column("photo_url", sa.String, nullable=True),
        sa.Column(
            "status",
            sa.Enum("active", "suspended", name="staff_status"),
            server_default="active",
            nullable=False,
        ),
        *_ts(),
    )
    op.create_index("ix_staff_business_id", "staff", ["business_id"])
    op.create_index("ix_staff_branch_id", "staff", ["branch_id"])
    op.create_index("ix_staff_user_id", "staff", ["user_id"])

    # ── schedules — weekly working pattern per staff member ─────────────────
    op.create_table(
        "schedules",
        _pk(),
        sa.Column("staff_id", sa.String, nullable=False),
        sa.Column("day_of_week", sa.Integer, nullable=False),  # 0=Mon … 6=Sun
        sa.Column("start_time", sa.Time, nullable=False),
        sa.Column("end_time", sa.Time, nullable=False),
    )
    op.create_index("ix_schedules_staff_id", "schedules", ["staff_id"])

    # ── attendance — check-in/out records ───────────────────────────────────
    op.create_table(
        "attendance",
        _pk(),
        sa.Column("staff_id", sa.String, nullable=False),
        sa.Column("branch_id", sa.String, nullable=False),
        sa.Column("work_date", sa.Date, nullable=False),
        sa.Column("check_in_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("check_out_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_attendance_staff_id", "attendance", ["staff_id"])
    op.create_index("ix_attendance_branch_id", "attendance", ["branch_id"])

    # ── leave_requests — barber asks, owner approves ───────────────────────
    op.create_table(
        "leave_requests",
        _pk(),
        sa.Column("staff_id", sa.String, nullable=False),
        sa.Column(
            "leave_type",
            sa.Enum("annual", "sick", "other", name="leave_type"),
            nullable=False,
        ),
        sa.Column("start_date", sa.Date, nullable=False),
        sa.Column("end_date", sa.Date, nullable=False),
        sa.Column("reason", sa.Text, nullable=True),
        sa.Column(
            "status",
            sa.Enum("pending", "approved", "rejected", name="leave_status"),
            server_default="pending",
            nullable=False,
        ),
        sa.Column("decided_by", sa.String, nullable=True),
        *_ts(),
    )
    op.create_index("ix_leave_requests_staff_id", "leave_requests", ["staff_id"])
    op.create_index("ix_leave_requests_decided_by", "leave_requests", ["decided_by"])

    # ── chairs — physical chairs per branch ─────────────────────────────────
    op.create_table(
        "chairs",
        _pk(),
        sa.Column("branch_id", sa.String, nullable=False),
        sa.Column("chair_number", sa.SmallInteger, nullable=False),
        sa.Column("assigned_staff_id", sa.String, nullable=True),
        sa.Column(
            "status",
            sa.Enum("free", "occupied", "reserved", "offline", name="chair_status"),
            server_default="free",
            nullable=False,
        ),
        *_ts(),
    )
    op.create_index("ix_chairs_branch_id", "chairs", ["branch_id"])
    op.create_index("ix_chairs_assigned_staff_id", "chairs", ["assigned_staff_id"])

    # ── appointments — walk-in / online / staff bookings ────────────────────
    op.create_table(
        "appointments",
        _pk(),
        sa.Column("business_id", sa.String, nullable=False),
        sa.Column("branch_id", sa.String, nullable=False),
        sa.Column("customer_id", sa.String, nullable=False),
        sa.Column("staff_id", sa.String, nullable=False),
        sa.Column("chair_id", sa.String, nullable=True),
        sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("duration_min", sa.SmallInteger, nullable=False),
        sa.Column(
            "source",
            sa.Enum("walk_in", "online", "staff", name="appointment_source"),
            server_default="staff",
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.Enum(
                "booked",
                "confirmed",
                "in_service",
                "completed",
                "no_show",
                "cancelled",
                name="appointment_status",
            ),
            server_default="booked",
            nullable=False,
        ),
        sa.Column("notes", sa.Text, nullable=True),
        *_ts(),
    )
    op.create_index("ix_appointments_business_id", "appointments", ["business_id"])
    op.create_index("ix_appointments_branch_id", "appointments", ["branch_id"])
    op.create_index("ix_appointments_customer_id", "appointments", ["customer_id"])
    op.create_index("ix_appointments_staff_id", "appointments", ["staff_id"])
    op.create_index("ix_appointments_chair_id", "appointments", ["chair_id"])
    op.create_index(
        "ix_appt_branch_scheduled", "appointments", ["branch_id", "scheduled_at"]
    )
    op.create_index(
        "ix_appt_staff_scheduled", "appointments", ["staff_id", "scheduled_at"]
    )

    # ── appointment_services — priced service lines on a booking ────────────
    op.create_table(
        "appointment_services",
        _pk(),
        sa.Column("appointment_id", sa.String, nullable=False),
        sa.Column("service_id", sa.String, nullable=False),
        sa.Column("price_kes", sa.Integer, nullable=False),
        *_ts(),
    )
    op.create_index(
        "ix_appointment_services_appointment_id",
        "appointment_services",
        ["appointment_id"],
    )
    op.create_index(
        "ix_appointment_services_service_id", "appointment_services", ["service_id"]
    )

    # ── queue_entries — the live walk-in queue ──────────────────────────────
    op.create_table(
        "queue_entries",
        _pk(),
        sa.Column("branch_id", sa.String, nullable=False),
        sa.Column("customer_id", sa.String, nullable=False),
        sa.Column("staff_id", sa.String, nullable=True),
        sa.Column("chair_id", sa.String, nullable=True),
        sa.Column("queue_number", sa.SmallInteger, nullable=False),
        sa.Column(
            "status",
            sa.Enum("waiting", "serving", "served", "left", name="queue_status"),
            server_default="waiting",
            nullable=False,
        ),
        sa.Column("joined_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("served_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("est_wait_min", sa.SmallInteger, nullable=True),
        *_ts(),
    )
    op.create_index("ix_queue_entries_branch_id", "queue_entries", ["branch_id"])
    op.create_index("ix_queue_entries_customer_id", "queue_entries", ["customer_id"])
    op.create_index("ix_queue_entries_staff_id", "queue_entries", ["staff_id"])
    op.create_index("ix_queue_entries_chair_id", "queue_entries", ["chair_id"])
    op.create_index(
        "ix_queue_branch_status_num",
        "queue_entries",
        ["branch_id", "status", "queue_number"],
    )


def downgrade() -> None:
    op.drop_table("queue_entries")
    op.drop_table("appointment_services")
    op.drop_table("appointments")
    op.drop_table("chairs")
    op.drop_table("attendance")
    op.drop_table("schedules")
    op.drop_table("leave_requests")
    op.drop_table("staff")
    op.drop_table("customers")
