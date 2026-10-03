"""Application configuration — loaded from environment (Doc 13 env template)."""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # General
    env: str = "production"
    secret_key: str
    access_token_ttl_min: int = 15
    refresh_token_ttl_days: int = 30
    allowed_origins: str = "https://groomio.app"

    # Database
    database_url: str
    db_pool_size: int = 10
    db_max_overflow: int = 20

    # Redis
    redis_url: str = "redis://localhost:6379/0"

    # Africa's Talking — OTP phone verification ONLY (MVP)
    at_api_key: str = ""
    at_username: str = "groomio"
    at_sender_id: str = "GROOMIO"

    # M-Pesa Daraja
    mpesa_consumer_key: str = ""
    mpesa_consumer_secret: str = ""
    mpesa_base_url: str = "https://sandbox.safaricom.co.ke"
    mpesa_stk_push_url: str = "https://sandbox.safaricom.co.ke/mpesa/stkpush/v2/query"
    mpesa_callback_url: str = "https://api.groomio.app/v1/payments/webhooks/daraja"
    # The paybill/till number Safaricom expects, plus the passkey issued with it.
    mpesa_shortcode: str = ""
    mpesa_passkey: str = ""

    # Pesapal (Airtel Money + cards early)
    pesapal_consumer_key: str = ""
    pesapal_consumer_secret: str = ""
    # The merchant_id is part of the IPN signature, so it is required even when
    # only the IPN endpoint is used.
    pesapal_merchant_id: str = ""
    pesapal_base_url: str = "https://sandbox.pesapal.com"
    pesapal_ipn_url: str = "https://api.groomio.app/v1/payments/webhooks/pesapal"
    pesapal_return_url: str = "https://app.groomio.app/settings/billing?status=paid"
    pesapal_cancel_url: str = (
        "https://app.groomio.app/settings/billing?status=cancelled"
    )

    # Cloudflare R2
    r2_account_id: str = ""
    r2_access_key_id: str = ""
    r2_secret_access_key: str = ""
    r2_bucket: str = "groomio"
    r2_signed_url_ttl_seconds: int = 300

    # Web push
    vapid_public_key: str = ""
    vapid_private_key: str = ""
    vapid_subject: str = "mailto:support@groomio.app"

    # Observability
    sentry_dsn: str = ""
    metabase_database_password: str = ""

    # Pricing (formula-driven — Doc 1 § pricing check)
    daily_price_kes: int = 40
    discount_biweekly_pct: int = 11
    discount_monthly_pct: int = 17
    discount_half_yearly_pct: int = 25
    discount_yearly_pct: int = 33

    # Trial / grace / reminders
    trial_days: int = 10
    grace_days: int = 3
    remind_trial_days: str = "7,9,10"
    remind_before_renewal_days: int = 3

    # OTP
    otp_ttl_min: int = 10

    # Email delivery — used for the second OTP channel (Doc 10.2 A2).
    #
    # OTP by email is the fallback for a customer whose phone is unreachable:
    # the code is the same 6 digits, the security model is identical, and the
    # address is already on the user row. Two independent transports, one
    # verification flow.
    #
    # SMTP credentials are NEVER committed. When empty, email OTP is disabled
    # and the request endpoint returns an error rather than silently queueing
    # a code that will never arrive.
    email_host: str = ""
    email_port: int = 587
    email_user: str = ""
    email_pass: str = ""
    email_from: str = "Groomio <lassotechnologies@gmail.com>"
    email_use_tls: bool = True

    # Compliance (Kenya DPA 2019)
    privacy_policy_url: str = "https://groomio.app/privacy"
    terms_url: str = "https://groomio.app/terms"
    chat_retention_months: int = 12
    backup_retention_days: int = 30

    model_config = SettingsConfigDict(env_prefix="", env_file=".env", extra="ignore")

    @property
    def allowed_origins_list(self) -> list[str]:
        return [o.strip() for o in self.allowed_origins.split(",") if o.strip()]

    @property
    def remind_trial_days_list(self) -> list[int]:
        return [int(d) for d in self.remind_trial_days.split(",") if d.strip()]


settings = Settings()
