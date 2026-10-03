"""SQLAlchemy models — the tables of Doc 9.

Naming rule: snake_case; every table has `id` (UUID PK), `created_at`,
`updated_at` unless noted. Multi-tenancy rule: business-scoped tables carry
`business_id`; branch-scoped tables carry `branch_id`.
"""

from app.models.identity import (
    User,
    OTPCode,
    RefreshToken,
    DeviceToken,
)
from app.models.tenant import (
    Business,
    Branch,
    Service,
)
from app.models.people import (
    Customer,
    Staff,
    Schedule,
    Attendance,
    LeaveRequest,
)
from app.models.operations import (
    Chair,
    Appointment,
    AppointmentService,
    QueueEntry,
)
from app.models.money import (
    Sale,
    SaleItem,
    Expense,
    Commission,
)
from app.models.inventory import Product, Supplier
from app.models.loyalty import (
    LoyaltyTransaction,
    MembershipPlan,
    CustomerMembership,
    Referral,
)
from app.models.billing import Plan, Subscription, Payment
from app.models.messaging import (
    MessageThread,
    Message,
    Notification,
    Campaign,
    CampaignRecipient,
)
from app.models.ai import AIProject, AIAsset
from app.models.platform import AuditLog, WebhookEvent

__all__ = [
    # identity
    "User",
    "OTPCode",
    "RefreshToken",
    "DeviceToken",
    # tenant
    "Business",
    "Branch",
    "Service",
    # people
    "Customer",
    "Staff",
    "Schedule",
    "Attendance",
    "LeaveRequest",
    # operations
    "Chair",
    "Appointment",
    "AppointmentService",
    "QueueEntry",
    # money
    "Sale",
    "SaleItem",
    "Expense",
    "Commission",
    # inventory
    "Product",
    "Supplier",
    # loyalty
    "LoyaltyTransaction",
    "MembershipPlan",
    "CustomerMembership",
    "Referral",
    # billing
    "Plan",
    "Subscription",
    "Payment",
    # messaging
    "MessageThread",
    "Message",
    "Notification",
    "Campaign",
    "CampaignRecipient",
    # ai
    "AIProject",
    "AIAsset",
    # platform
    "AuditLog",
    "WebhookEvent",
]
