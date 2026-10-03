"""money, inventory, loyalty, payments, messaging, ai and platform tables

Completes the 30-table schema of Doc 9: sales, sale_items, expenses, commissions,
products, suppliers, loyalty_transactions, membership_plans, customer_memberships,
referrals, payments, message_threads, messages, notifications, campaigns,
campaign_recipients, ai_projects, ai_assets, audit_logs, webhook_events.

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-28
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

# revision identifiers, used by Alembic.
revision = "0005"
down_revision = "0004"
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
    # ── 9.5 Money ───────────────────────────────────────────────────────────
    op.create_table(
        "sales",
        _pk(),
        sa.Column("business_id", sa.String, nullable=False),
        sa.Column("branch_id", sa.String, nullable=False),
        sa.Column("customer_id", sa.String, nullable=True),
        sa.Column("staff_id", sa.String, nullable=False),
        sa.Column("total_kes", sa.Integer, nullable=False),
        sa.Column(
            "payment_method",
            sa.Enum("cash", "mpesa", "pesapal", name="payment_method"),
            server_default="cash",
            nullable=False,
        ),
        sa.Column("created_by", sa.String, nullable=False),
        *_ts(),
    )
    op.create_index("ix_sales_business_id", "sales", ["business_id"])
    op.create_index("ix_sales_branch_id", "sales", ["branch_id"])
    op.create_index("ix_sales_customer_id", "sales", ["customer_id"])
    op.create_index("ix_sales_staff_id", "sales", ["staff_id"])
    op.create_index("ix_sales_created_by", "sales", ["created_by"])
    op.create_index("ix_sales_biz_paid", "sales", ["business_id", "created_at"])

    # sale_items also doubles as the commission record (Doc 9.5).
    op.create_table(
        "sale_items",
        _pk(),
        sa.Column("sale_id", sa.String, nullable=False),
        sa.Column(
            "item_type", sa.Enum("service", "product", name="item_type"), nullable=False
        ),
        sa.Column("service_id", sa.String, nullable=True),
        sa.Column("product_id", sa.String, nullable=True),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("price_kes", sa.Integer, nullable=False),
        sa.Column("quantity", sa.Integer, server_default="1", nullable=False),
        sa.Column("commission_kes", sa.Integer, server_default="0", nullable=False),
        *_ts(),
    )
    op.create_index("ix_sale_items_sale_id", "sale_items", ["sale_id"])
    op.create_index("ix_sale_items_service_id", "sale_items", ["service_id"])
    op.create_index("ix_sale_items_product_id", "sale_items", ["product_id"])

    op.create_table(
        "expenses",
        _pk(),
        sa.Column("branch_id", sa.String, nullable=False),
        sa.Column(
            "category",
            sa.Enum(
                "rent",
                "stock",
                "utilities",
                "salaries",
                "other",
                name="expense_category",
            ),
            nullable=False,
        ),
        sa.Column("amount_kes", sa.Integer, nullable=False),
        sa.Column("note", sa.String, nullable=True),
        sa.Column("incurred_at", sa.Date, nullable=False),
        sa.Column("recorded_by", sa.String, nullable=False),
        *_ts(),
    )
    op.create_index("ix_expenses_branch_id", "expenses", ["branch_id"])
    op.create_index("ix_expenses_recorded_by", "expenses", ["recorded_by"])

    op.create_table(
        "commissions",
        _pk(),
        sa.Column("staff_id", sa.String, nullable=False),
        sa.Column("branch_id", sa.String, nullable=False),
        sa.Column("period_start", sa.Date, nullable=False),
        sa.Column("period_end", sa.Date, nullable=False),
        sa.Column("earned_kes", sa.Integer, server_default="0", nullable=False),
        sa.Column("bonus_kes", sa.Integer, server_default="0", nullable=False),
        sa.Column(
            "status",
            sa.Enum("open", "paid", name="commission_status"),
            server_default="open",
            nullable=False,
        ),
        *_ts(),
    )
    op.create_index("ix_commissions_staff_id", "commissions", ["staff_id"])
    op.create_index("ix_commissions_branch_id", "commissions", ["branch_id"])

    # ── 9.6 Inventory ───────────────────────────────────────────────────────
    op.create_table(
        "products",
        _pk(),
        sa.Column("branch_id", sa.String, nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("category", sa.String(80), nullable=True),
        sa.Column("cost_price_kes", sa.Integer, server_default="0", nullable=False),
        sa.Column("sell_price_kes", sa.Integer, nullable=False),
        sa.Column("stock_qty", sa.Integer, server_default="0", nullable=False),
        sa.Column(
            "low_stock_threshold", sa.Integer, server_default="5", nullable=False
        ),
        *_ts(),
    )
    op.create_index("ix_products_branch_id", "products", ["branch_id"])

    op.create_table(
        "suppliers",
        _pk(),
        sa.Column("business_id", sa.String, nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("phone", sa.String(15), nullable=True),
        sa.Column("last_order_at", sa.Date, nullable=True),
        *_ts(),
    )
    op.create_index("ix_suppliers_business_id", "suppliers", ["business_id"])

    # ── 9.7 Loyalty & memberships ───────────────────────────────────────────
    op.create_table(
        "loyalty_transactions",
        _pk(),
        sa.Column("customer_id", sa.String, nullable=False),
        sa.Column("points", sa.Integer, nullable=False),
        sa.Column(
            "reason",
            sa.Enum(
                "visit", "referral", "birthday", "redemption", name="loyalty_reason"
            ),
            nullable=False,
        ),
        sa.Column("sale_id", sa.String, nullable=True),
        *_ts(),
    )
    op.create_index(
        "ix_loyalty_transactions_customer_id", "loyalty_transactions", ["customer_id"]
    )
    op.create_index(
        "ix_loyalty_transactions_sale_id", "loyalty_transactions", ["sale_id"]
    )

    op.create_table(
        "membership_plans",
        _pk(),
        sa.Column("branch_id", sa.String, nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("price_kes", sa.Integer, nullable=False),
        sa.Column("sessions_included", sa.SmallInteger, nullable=False),
        sa.Column("validity_days", sa.SmallInteger, nullable=False),
        *_ts(),
    )
    op.create_index("ix_membership_plans_branch_id", "membership_plans", ["branch_id"])

    op.create_table(
        "customer_memberships",
        _pk(),
        sa.Column("customer_id", sa.String, nullable=False),
        sa.Column("plan_id", sa.String, nullable=False),
        sa.Column("started_at", sa.Date, nullable=False),
        sa.Column("expires_at", sa.Date, nullable=False),
        sa.Column("sessions_used", sa.SmallInteger, server_default="0", nullable=False),
        *_ts(),
    )
    op.create_index(
        "ix_customer_memberships_customer_id", "customer_memberships", ["customer_id"]
    )
    op.create_index(
        "ix_customer_memberships_plan_id", "customer_memberships", ["plan_id"]
    )

    op.create_table(
        "referrals",
        _pk(),
        sa.Column("referrer_customer_id", sa.String, nullable=False),
        sa.Column("referred_phone", sa.String(15), nullable=False),
        sa.Column(
            "status",
            sa.Enum("pending", "converted", "rewarded", name="referral_status"),
            server_default="pending",
            nullable=False,
        ),
        sa.Column("points_awarded", sa.Integer, server_default="0", nullable=False),
        *_ts(),
    )
    op.create_index(
        "ix_referrals_referrer_customer_id", "referrals", ["referrer_customer_id"]
    )

    # ── 9.8 Payments ────────────────────────────────────────────────────────
    op.create_table(
        "payments",
        _pk(),
        sa.Column("business_id", sa.String, nullable=False),
        sa.Column("subscription_id", sa.String, nullable=False),
        sa.Column("amount_kes", sa.Integer, nullable=False),
        sa.Column(
            "method",
            sa.Enum(
                "mpesa",
                "airtel_money",
                "bank_transfer",
                "pesapal",
                name="payment_method_sub",
            ),
            nullable=False,
        ),
        sa.Column(
            "provider",
            sa.Enum("daraja", "pesapal", "manual", name="payment_provider"),
            nullable=False,
        ),
        sa.Column("provider_ref", sa.String, nullable=True),
        sa.Column(
            "status",
            sa.Enum("pending", "success", "failed", "timeout", name="payment_status"),
            server_default="pending",
            nullable=False,
        ),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("raw_callback", JSONB, nullable=True),
        *_ts(),
    )
    op.create_index("ix_payments_business_id", "payments", ["business_id"])
    op.create_index("ix_payments_subscription_id", "payments", ["subscription_id"])
    op.create_index("ix_payments_biz_paid", "payments", ["business_id", "paid_at"])

    # ── 9.9 Messaging, campaigns & AI Studio ────────────────────────────────
    op.create_table(
        "message_threads",
        _pk(),
        sa.Column("branch_id", sa.String, nullable=False),
        sa.Column("customer_id", sa.String, nullable=False),
        sa.Column("appointment_id", sa.String, nullable=True),
        sa.Column("queue_entry_id", sa.String, nullable=True),
        sa.Column(
            "status",
            sa.Enum("open", "archived", name="thread_status"),
            server_default="open",
            nullable=False,
        ),
        *_ts(),
    )
    op.create_index("ix_message_threads_branch_id", "message_threads", ["branch_id"])
    op.create_index(
        "ix_message_threads_customer_id", "message_threads", ["customer_id"]
    )
    op.create_index(
        "ix_message_threads_appointment_id", "message_threads", ["appointment_id"]
    )
    op.create_index(
        "ix_message_threads_queue_entry_id", "message_threads", ["queue_entry_id"]
    )

    op.create_table(
        "messages",
        _pk(),
        sa.Column("thread_id", sa.String, nullable=False),
        sa.Column("sender_user_id", sa.String, nullable=True),
        sa.Column(
            "sender_side",
            sa.Enum("shop", "customer", name="sender_side"),
            nullable=False,
        ),
        sa.Column("body", sa.Text, nullable=False),
        sa.Column("attachment_url", sa.String, nullable=True),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        *_ts(),
    )
    op.create_index("ix_messages_thread_id", "messages", ["thread_id"])
    op.create_index("ix_messages_sender_user_id", "messages", ["sender_user_id"])

    op.create_table(
        "notifications",
        _pk(),
        sa.Column("recipient_user_id", sa.String, nullable=True),
        sa.Column("recipient_customer_id", sa.String, nullable=True),
        sa.Column(
            "type",
            sa.Enum(
                "appointment",
                "queue",
                "subscription",
                "birthday",
                "campaign",
                name="notification_type",
            ),
            nullable=False,
        ),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("body", sa.Text, nullable=False),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        *_ts(),
    )
    op.create_index(
        "ix_notifications_recipient_user_id", "notifications", ["recipient_user_id"]
    )
    op.create_index(
        "ix_notifications_recipient_customer_id",
        "notifications",
        ["recipient_customer_id"],
    )

    # MVP campaigns are in-app + push only — SMS returns in Phase 2 (Doc 1).
    op.create_table(
        "campaigns",
        _pk(),
        sa.Column("business_id", sa.String, nullable=False),
        sa.Column(
            "channel",
            sa.Enum("in_app", "push", name="campaign_channel"),
            nullable=False,
        ),
        sa.Column(
            "segment",
            sa.Enum(
                "all",
                "inactive_30d",
                "birthday_month",
                "loyalty_tier",
                name="campaign_segment",
            ),
            nullable=False,
        ),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("body", sa.Text, nullable=False),
        sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "status",
            sa.Enum("draft", "scheduled", "sending", "sent", name="campaign_status"),
            server_default="draft",
            nullable=False,
        ),
        *_ts(),
    )
    op.create_index("ix_campaigns_business_id", "campaigns", ["business_id"])

    op.create_table(
        "campaign_recipients",
        _pk(),
        sa.Column("campaign_id", sa.String, nullable=False),
        sa.Column("customer_id", sa.String, nullable=False),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        *_ts(),
    )
    op.create_index(
        "ix_campaign_recipients_campaign_id", "campaign_recipients", ["campaign_id"]
    )
    op.create_index(
        "ix_campaign_recipients_customer_id", "campaign_recipients", ["customer_id"]
    )

    op.create_table(
        "ai_projects",
        _pk(),
        sa.Column("business_id", sa.String, nullable=False),
        sa.Column("shop_name", sa.String(160), nullable=False),
        sa.Column("logo_url", sa.String, nullable=True),
        sa.Column("photo_urls", JSONB, nullable=False),  # ≥5 photos
        sa.Column(
            "status",
            sa.Enum("uploaded", "processing", "ready", name="ai_project_status"),
            server_default="uploaded",
            nullable=False,
        ),
        *_ts(),
    )
    op.create_index("ix_ai_projects_business_id", "ai_projects", ["business_id"])

    op.create_table(
        "ai_assets",
        _pk(),
        sa.Column("project_id", sa.String, nullable=False),
        sa.Column(
            "asset_type",
            sa.Enum(
                "fb_post",
                "ig_post",
                "whatsapp_status",
                "tiktok_caption",
                "poster",
                "slideshow_video",
                name="ai_asset_type",
            ),
            nullable=False,
        ),
        sa.Column("content_text", sa.Text, nullable=True),
        sa.Column("file_url", sa.String, nullable=True),
        *_ts(),
    )
    op.create_index("ix_ai_assets_project_id", "ai_assets", ["project_id"])

    # ── 9.10 Platform ───────────────────────────────────────────────────────
    op.create_table(
        "audit_logs",
        _pk(),
        sa.Column("business_id", sa.String, nullable=True),  # null = platform-level
        sa.Column("actor_user_id", sa.String, nullable=False),
        sa.Column("action", sa.String(120), nullable=False),
        sa.Column("entity", sa.String(80), nullable=True),
        sa.Column("entity_id", sa.String, nullable=True),
        sa.Column("before_json", JSONB, nullable=True),
        sa.Column("after_json", JSONB, nullable=True),
        *_ts(),
    )
    op.create_index("ix_audit_logs_business_id", "audit_logs", ["business_id"])
    op.create_index("ix_audit_logs_actor_user_id", "audit_logs", ["actor_user_id"])
    op.create_index("ix_audit_logs_entity_id", "audit_logs", ["entity_id"])

    # Raw callbacks land here first so nothing is lost if a worker is down.
    op.create_table(
        "webhook_events",
        _pk(),
        sa.Column(
            "source",
            sa.Enum("daraja", "pesapal", name="webhook_source"),
            nullable=False,
        ),
        sa.Column("event_ref", sa.String, nullable=False),
        sa.Column("payload", JSONB, nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        *_ts(),
    )
    op.create_index("ix_webhook_events_event_ref", "webhook_events", ["event_ref"])
    op.create_index(
        "ix_webhook_events_unprocessed", "webhook_events", ["source", "processed_at"]
    )


def downgrade() -> None:
    for table in (
        "webhook_events",
        "audit_logs",
        "ai_assets",
        "ai_projects",
        "campaign_recipients",
        "campaigns",
        "notifications",
        "messages",
        "message_threads",
        "payments",
        "referrals",
        "customer_memberships",
        "membership_plans",
        "loyalty_transactions",
        "suppliers",
        "products",
        "commissions",
        "expenses",
        "sale_items",
        "sales",
    ):
        op.drop_table(table)
