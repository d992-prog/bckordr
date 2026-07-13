from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, utcnow


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(32), default="user", server_default="user")
    status: Mapped[str] = mapped_column(String(32), default="pending", server_default="pending")
    language: Mapped[str] = mapped_column(String(8), default="ru", server_default="ru")
    max_domains: Mapped[int | None] = mapped_column(Integer, nullable=True)
    access_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    telegram_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    telegram_chat_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    login_failed_attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    login_locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        onupdate=utcnow,
        server_default=func.now(),
    )

    domains: Mapped[list["Domain"]] = relationship(back_populates="owner", cascade="all, delete-orphan")
    proxies: Mapped[list["Proxy"]] = relationship(back_populates="owner", cascade="all, delete-orphan")
    sessions: Mapped[list["UserSession"]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
    )
    promo_redemptions: Mapped[list["PromoRedemption"]] = relationship(back_populates="user")


class Domain(Base):
    __tablename__ = "domains"
    __table_args__ = (UniqueConstraint("owner_id", "domain", name="uq_domains_owner_domain"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    owner_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    domain: Mapped[str] = mapped_column(String(255), index=True)
    zone: Mapped[str] = mapped_column(String(16), default="fr", server_default="fr")
    status: Mapped[str] = mapped_column(String(32), default="active", server_default="active")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    manual_burst: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    scheduler_mode: Mapped[str] = mapped_column(
        String(32),
        default="continuous",
        server_default="continuous",
    )
    check_interval: Mapped[float] = mapped_column(Float, default=1.5, server_default="1.5")
    burst_check_interval: Mapped[float] = mapped_column(Float, default=0.35, server_default="0.35")
    pattern_slow_interval: Mapped[float] = mapped_column(Float, default=60.0, server_default="60.0")
    pattern_fast_interval: Mapped[float] = mapped_column(Float, default=0.5, server_default="0.5")
    pattern_window_start_minute: Mapped[int] = mapped_column(Integer, default=31, server_default="31")
    pattern_window_end_minute: Mapped[int] = mapped_column(Integer, default=34, server_default="34")
    confirmation_threshold: Mapped[int] = mapped_column(Integer, default=3, server_default="3")
    available_recheck_enabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    available_recheck_interval: Mapped[float] = mapped_column(
        Float,
        default=1800.0,
        server_default="1800.0",
    )
    check_mode: Mapped[str] = mapped_column(String(16), default="normal", server_default="normal")
    last_check_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_cycle_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    worker_heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    available_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_seen_owner: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_seen_rdap_status: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_owner_change_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    available_confirmations: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    alert_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        onupdate=utcnow,
        server_default=func.now(),
    )

    owner: Mapped[User | None] = relationship(back_populates="domains")
    logs: Mapped[list["Log"]] = relationship(
        back_populates="domain_ref",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class Proxy(Base):
    __tablename__ = "proxies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    owner_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    host: Mapped[str] = mapped_column(String(255))
    port: Mapped[int] = mapped_column(Integer)
    login: Mapped[str | None] = mapped_column(String(255), nullable=True)
    password: Mapped[str | None] = mapped_column(String(255), nullable=True)
    type: Mapped[str] = mapped_column(String(32), default="socks5", server_default="socks5")
    status: Mapped[str] = mapped_column(String(32), default="active", server_default="active")
    fail_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    last_used: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        server_default=func.now(),
    )

    owner: Mapped[User | None] = relationship(back_populates="proxies")


class Log(Base):
    __tablename__ = "logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    owner_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    domain_id: Mapped[int | None] = mapped_column(
        ForeignKey("domains.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    event_type: Mapped[str] = mapped_column(String(32))
    message: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        server_default=func.now(),
    )

    domain_ref: Mapped[Domain | None] = relationship(back_populates="logs")


class UserSession(Base):
    __tablename__ = "user_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    token_hash: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    remember_me: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        server_default=func.now(),
    )

    user: Mapped[User] = relationship(back_populates="sessions")


class PromoCode(Base):
    __tablename__ = "promo_codes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    duration_seconds: Mapped[int] = mapped_column(Integer)
    max_activations: Mapped[int | None] = mapped_column(Integer, nullable=True)
    activation_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        onupdate=utcnow,
        server_default=func.now(),
    )

    redemptions: Mapped[list["PromoRedemption"]] = relationship(
        back_populates="promo_code",
        cascade="all, delete-orphan",
    )


class PromoRedemption(Base):
    __tablename__ = "promo_redemptions"
    __table_args__ = (UniqueConstraint("promo_code_id", "user_id", name="uq_promo_code_user"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    promo_code_id: Mapped[int] = mapped_column(ForeignKey("promo_codes.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    duration_seconds: Mapped[int] = mapped_column(Integer)
    redeemed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        server_default=func.now(),
    )

    promo_code: Mapped[PromoCode] = relationship(back_populates="redemptions")
    user: Mapped[User] = relationship(back_populates="promo_redemptions")


class AdminAuditLog(Base):
    __tablename__ = "admin_audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    actor_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    target_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    action: Mapped[str] = mapped_column(String(64))
    details: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        server_default=func.now(),
    )


class AppSetting(Base):
    __tablename__ = "app_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    key: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    value: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        onupdate=utcnow,
        server_default=func.now(),
    )


class ContactProfile(Base):
    __tablename__ = "contact_profiles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    label: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    person_type: Mapped[str] = mapped_column(String(32), default="individual", server_default="individual")
    given_name: Mapped[str] = mapped_column(String(128))
    family_name: Mapped[str] = mapped_column(String(128))
    organization_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    email: Mapped[str] = mapped_column(String(255))
    phone: Mapped[str] = mapped_column(String(64))
    mobile: Mapped[str | None] = mapped_column(String(64), nullable=True)
    fax: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lang: Mapped[str | None] = mapped_column(String(16), nullable=True)
    street_address: Mapped[str] = mapped_column(String(255))
    city: Mapped[str] = mapped_column(String(128))
    state: Mapped[str | None] = mapped_column(String(128), nullable=True)
    zip_code: Mapped[str] = mapped_column(String(32))
    country_code: Mapped[str] = mapped_column(String(8), default="FR", server_default="FR")
    data_obfuscated: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    mail_obfuscated: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    icann_contract_accept: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    extra_parameters: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        onupdate=utcnow,
        server_default=func.now(),
    )


class RegistrarAccount(Base):
    __tablename__ = "registrar_accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    registrar_slug: Mapped[str] = mapped_column(String(64), default="gandi", server_default="gandi")
    api_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    api_base_url: Mapped[str | None] = mapped_column(String(255), nullable=True)
    sharing_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    default_contact_profile_id: Mapped[int | None] = mapped_column(
        ForeignKey("contact_profiles.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    supports_dry_run: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    last_validation_status: Mapped[str] = mapped_column(
        String(32),
        default="unknown",
        server_default="unknown",
    )
    last_validation_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_validated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        onupdate=utcnow,
        server_default=func.now(),
    )

    default_contact_profile: Mapped[ContactProfile | None] = relationship()


class ZoneStrategy(Base):
    __tablename__ = "zone_strategies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    zone: Mapped[str] = mapped_column(String(32), index=True)
    name: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    timezone_name: Mapped[str] = mapped_column(String(64), default="UTC", server_default="UTC")
    rule_resolution_mode: Mapped[str] = mapped_column(String(32), default="priority", server_default="priority")
    default_min_guaranteed_rps: Mapped[float] = mapped_column(Float, default=1.0, server_default="1.0")
    default_registrar_slug: Mapped[str] = mapped_column(String(64), default="gandi", server_default="gandi")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        onupdate=utcnow,
        server_default=func.now(),
    )


class ZoneRule(Base):
    __tablename__ = "zone_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    zone_strategy_id: Mapped[int] = mapped_column(ForeignKey("zone_strategies.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(128))
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    priority: Mapped[int] = mapped_column(Integer, default=100, server_default="100")
    schedule_type: Mapped[str] = mapped_column(String(32), default="hourly", server_default="hourly")
    hour: Mapped[int | None] = mapped_column(Integer, nullable=True)
    minute: Mapped[int] = mapped_column(Integer, default=31, server_default="31")
    second: Mapped[int] = mapped_column(Integer, default=59, server_default="59")
    weekdays: Mapped[str | None] = mapped_column(String(64), nullable=True)
    specific_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    window_duration_seconds: Mapped[int] = mapped_column(Integer, default=61, server_default="61")
    execution_profile_mode: Mapped[str] = mapped_column(String(32), default="flat", server_default="flat")
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        onupdate=utcnow,
        server_default=func.now(),
    )

    zone_strategy: Mapped[ZoneStrategy] = relationship()


class ZoneRulePhase(Base):
    __tablename__ = "zone_rule_phases"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    zone_rule_id: Mapped[int] = mapped_column(ForeignKey("zone_rules.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(64))
    sort_order: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    start_offset_seconds: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    duration_seconds: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    rps_mode: Mapped[str] = mapped_column(String(32), default="percent", server_default="percent")
    rps_value: Mapped[float] = mapped_column(Float, default=100.0, server_default="100.0")
    stop_on_success: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        onupdate=utcnow,
        server_default=func.now(),
    )

    zone_rule: Mapped[ZoneRule] = relationship()


class DomainRuleOverride(Base):
    __tablename__ = "domain_rule_overrides"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    timezone_name: Mapped[str] = mapped_column(String(64), default="UTC", server_default="UTC")
    rule_resolution_mode: Mapped[str] = mapped_column(String(32), default="priority", server_default="priority")
    default_min_guaranteed_rps: Mapped[float] = mapped_column(Float, default=1.0, server_default="1.0")
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        onupdate=utcnow,
        server_default=func.now(),
    )


class DomainOverrideRule(Base):
    __tablename__ = "domain_override_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    domain_rule_override_id: Mapped[int] = mapped_column(
        ForeignKey("domain_rule_overrides.id", ondelete="CASCADE"),
        index=True,
    )
    name: Mapped[str] = mapped_column(String(128))
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    priority: Mapped[int] = mapped_column(Integer, default=100, server_default="100")
    schedule_type: Mapped[str] = mapped_column(String(32), default="hourly", server_default="hourly")
    hour: Mapped[int | None] = mapped_column(Integer, nullable=True)
    minute: Mapped[int] = mapped_column(Integer, default=31, server_default="31")
    second: Mapped[int] = mapped_column(Integer, default=59, server_default="59")
    weekdays: Mapped[str | None] = mapped_column(String(64), nullable=True)
    specific_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    window_duration_seconds: Mapped[int] = mapped_column(Integer, default=61, server_default="61")
    execution_profile_mode: Mapped[str] = mapped_column(String(32), default="flat", server_default="flat")
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        onupdate=utcnow,
        server_default=func.now(),
    )

    domain_rule_override: Mapped[DomainRuleOverride] = relationship()


class DomainOverridePhase(Base):
    __tablename__ = "domain_override_phases"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    domain_override_rule_id: Mapped[int] = mapped_column(
        ForeignKey("domain_override_rules.id", ondelete="CASCADE"),
        index=True,
    )
    name: Mapped[str] = mapped_column(String(64))
    sort_order: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    start_offset_seconds: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    duration_seconds: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    rps_mode: Mapped[str] = mapped_column(String(32), default="percent", server_default="percent")
    rps_value: Mapped[float] = mapped_column(Float, default=100.0, server_default="100.0")
    stop_on_success: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        onupdate=utcnow,
        server_default=func.now(),
    )

    domain_override_rule: Mapped[DomainOverrideRule] = relationship()


class WorkerNode(Base):
    __tablename__ = "worker_nodes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    registrar_slug: Mapped[str] = mapped_column(String(64), default="gandi", server_default="gandi")
    assigned_registrar_account_id: Mapped[int | None] = mapped_column(
        ForeignKey("registrar_accounts.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    api_base_url: Mapped[str | None] = mapped_column(String(255), nullable=True)
    control_token: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="provisioning", server_default="provisioning")
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    ip_address: Mapped[str | None] = mapped_column(String(64), nullable=True)
    region: Mapped[str | None] = mapped_column(String(128), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    max_rps: Mapped[float] = mapped_column(Float, default=16.0, server_default="16.0")
    target_rps: Mapped[float] = mapped_column(Float, default=16.0, server_default="16.0")
    current_rps: Mapped[float] = mapped_column(Float, default=0.0, server_default="0.0")
    current_capacity_rps: Mapped[float] = mapped_column(Float, default=0.0, server_default="0.0")
    cpu_load: Mapped[float] = mapped_column(Float, default=0.0, server_default="0.0")
    ram_usage_percent: Mapped[float] = mapped_column(Float, default=0.0, server_default="0.0")
    clock_drift_ms: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    runtime_mode: Mapped[str] = mapped_column(String(32), default="unknown", server_default="unknown")
    current_domain_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        onupdate=utcnow,
        server_default=func.now(),
    )

    assigned_registrar_account: Mapped[RegistrarAccount | None] = relationship()


class DropDomain(Base):
    __tablename__ = "drop_domains"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    fqdn: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    zone: Mapped[str] = mapped_column(String(32), default="fr", server_default="fr")
    timezone_name: Mapped[str] = mapped_column(String(64), default="Europe/Paris", server_default="Europe/Paris")
    registrar_slug: Mapped[str] = mapped_column(String(64), default="gandi", server_default="gandi")
    zone_strategy_id: Mapped[int | None] = mapped_column(
        ForeignKey("zone_strategies.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    domain_rule_override_id: Mapped[int | None] = mapped_column(
        ForeignKey("domain_rule_overrides.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    strategy_mode: Mapped[str] = mapped_column(String(32), default="inherit_zone", server_default="inherit_zone")
    registrar_account_id: Mapped[int | None] = mapped_column(
        ForeignKey("registrar_accounts.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    contact_profile_id: Mapped[int | None] = mapped_column(
        ForeignKey("contact_profiles.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    drop_date: Mapped[date] = mapped_column(Date, index=True)
    priority: Mapped[int] = mapped_column(Integer, default=100, server_default="100")
    requested_duration_years: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    registration_extra_parameters: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="draft", server_default="draft")
    attack_enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    override_min_guaranteed_rps: Mapped[float | None] = mapped_column(Float, nullable=True)
    readiness_reasons: Mapped[str | None] = mapped_column(Text, nullable=True)
    window_start_minute: Mapped[int] = mapped_column(Integer, default=31, server_default="31")
    window_start_second: Mapped[int] = mapped_column(Integer, default=59, server_default="59")
    window_duration_seconds: Mapped[int] = mapped_column(Integer, default=61, server_default="61")
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    dry_run_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    dry_run_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    dry_run_http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    dry_run_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    success_worker_id: Mapped[int | None] = mapped_column(
        ForeignKey("worker_nodes.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    success_response_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    success_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        onupdate=utcnow,
        server_default=func.now(),
    )

    registrar_account: Mapped[RegistrarAccount | None] = relationship()
    contact_profile: Mapped[ContactProfile | None] = relationship()
    success_worker: Mapped[WorkerNode | None] = relationship()
    zone_strategy: Mapped[ZoneStrategy | None] = relationship()
    domain_rule_override: Mapped[DomainRuleOverride | None] = relationship()


class DiscoveryDomain(Base):
    __tablename__ = "discovery_domains"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    fqdn: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    zone: Mapped[str] = mapped_column(String(32), index=True)
    status: Mapped[str] = mapped_column(String(32), default="tracking", server_default="tracking", index=True)
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    check_interval_seconds: Mapped[int] = mapped_column(Integer, default=21600, server_default="21600")
    source_mode: Mapped[str] = mapped_column(String(32), default="rdap", server_default="rdap")
    drop_prediction_enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    last_lifecycle_stage: Mapped[str | None] = mapped_column(String(32), nullable=True)
    last_status_codes: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_availability: Mapped[str | None] = mapped_column(String(32), nullable=True)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    next_check_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    first_seen_redemption_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_seen_redemption_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    redemption_anchor_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    redemption_anchor_source: Mapped[str | None] = mapped_column(String(64), nullable=True)
    predicted_pending_delete_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    pending_delete_previous_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    first_seen_pending_delete_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_seen_pending_delete_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    predicted_drop_start_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    predicted_drop_end_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    available_first_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        onupdate=utcnow,
        server_default=func.now(),
    )


class DiscoveryObservation(Base):
    __tablename__ = "discovery_observations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    discovery_domain_id: Mapped[int] = mapped_column(
        ForeignKey("discovery_domains.id", ondelete="CASCADE"),
        index=True,
    )
    source: Mapped[str] = mapped_column(String(32))
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, server_default=func.now())
    http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    lifecycle_stage: Mapped[str | None] = mapped_column(String(32), nullable=True)
    availability_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    status_codes: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_response: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        server_default=func.now(),
    )

    discovery_domain: Mapped[DiscoveryDomain] = relationship()


class ZoneScanJob(Base):
    __tablename__ = "zone_scan_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    zone: Mapped[str] = mapped_column(String(32), index=True)
    source_type: Mapped[str] = mapped_column(String(32), default="zone_latest", server_default="zone_latest")
    source_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="queued", server_default="queued", index=True)
    file_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    file_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    download_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    file_size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    downloaded_bytes: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    scanned_lines: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    parsed_domains: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    filtered_candidates: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    submitted_rdap: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    completed_rdap: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    found_candidates: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    error_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    min_score: Mapped[int] = mapped_column(Integer, default=35, server_default="35")
    limit_output: Mapped[int] = mapped_column(Integer, default=20, server_default="20")
    max_rdap_checks: Mapped[int] = mapped_column(Integer, default=300000, server_default="300000")
    concurrency: Mapped[int] = mapped_column(Integer, default=100, server_default="100")
    rdap_timeout_seconds: Mapped[float] = mapped_column(Float, default=5.0, server_default="5.0")
    pending_delete_min_days: Mapped[float | None] = mapped_column(Float, nullable=True)
    pending_delete_max_days: Mapped[float | None] = mapped_column(Float, nullable=True)
    reservoir_size: Mapped[int] = mapped_column(Integer, default=300000, server_default="300000")
    random_seed: Mapped[int] = mapped_column(Integer, default=42, server_default="42")
    keep_file: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        onupdate=utcnow,
        server_default=func.now(),
    )


class ZoneScanCandidate(Base):
    __tablename__ = "zone_scan_candidates"
    __table_args__ = (UniqueConstraint("job_id", "fqdn", name="uq_zone_scan_candidates_job_fqdn"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("zone_scan_jobs.id", ondelete="CASCADE"), index=True)
    fqdn: Mapped[str] = mapped_column(String(255), index=True)
    zone: Mapped[str] = mapped_column(String(32), index=True)
    lifecycle_stage: Mapped[str] = mapped_column(String(32), index=True)
    status_codes: Mapped[str | None] = mapped_column(Text, nullable=True)
    http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    redemption_anchor_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    predicted_pending_delete_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    days_to_pending_delete: Mapped[float | None] = mapped_column(Float, nullable=True)
    score: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    discovery_domain_id: Mapped[int | None] = mapped_column(
        ForeignKey("discovery_domains.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    is_ignored: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        server_default=func.now(),
    )

    job: Mapped[ZoneScanJob] = relationship()
    discovery_domain: Mapped[DiscoveryDomain | None] = relationship()


class AttackRun(Base):
    __tablename__ = "attack_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    domain_id: Mapped[int] = mapped_column(ForeignKey("drop_domains.id", ondelete="CASCADE"), index=True)
    status: Mapped[str] = mapped_column(String(32), default="planned", server_default="planned")
    planned_start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    planned_end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    assigned_worker_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    planned_rps: Mapped[float] = mapped_column(Float, default=0.0, server_default="0.0")
    current_rps: Mapped[float] = mapped_column(Float, default=0.0, server_default="0.0")
    max_rps: Mapped[float] = mapped_column(Float, default=0.0, server_default="0.0")
    success_worker_id: Mapped[int | None] = mapped_column(
        ForeignKey("worker_nodes.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    stop_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        onupdate=utcnow,
        server_default=func.now(),
    )

    domain: Mapped[DropDomain] = relationship()
    success_worker: Mapped[WorkerNode | None] = relationship()


class WorkerTask(Base):
    __tablename__ = "worker_tasks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    attack_run_id: Mapped[int] = mapped_column(ForeignKey("attack_runs.id", ondelete="CASCADE"), index=True)
    domain_id: Mapped[int] = mapped_column(ForeignKey("drop_domains.id", ondelete="CASCADE"), index=True)
    worker_id: Mapped[int] = mapped_column(ForeignKey("worker_nodes.id", ondelete="CASCADE"), index=True)
    status: Mapped[str] = mapped_column(String(32), default="queued", server_default="queued")
    planned_rps: Mapped[float] = mapped_column(Float, default=0.0, server_default="0.0")
    actual_rps: Mapped[float] = mapped_column(Float, default=0.0, server_default="0.0")
    total_attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    success_attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    latency_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    last_http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    assigned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    stop_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        onupdate=utcnow,
        server_default=func.now(),
    )

    attack_run: Mapped[AttackRun] = relationship()
    domain: Mapped[DropDomain] = relationship()
    worker: Mapped[WorkerNode] = relationship()


class AttackEvent(Base):
    __tablename__ = "attack_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    attack_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("attack_runs.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    domain_id: Mapped[int | None] = mapped_column(
        ForeignKey("drop_domains.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    worker_id: Mapped[int | None] = mapped_column(
        ForeignKey("worker_nodes.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    level: Mapped[str] = mapped_column(String(16), default="info", server_default="info")
    event_type: Mapped[str] = mapped_column(String(64))
    message: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        server_default=func.now(),
    )

    attack_run: Mapped[AttackRun | None] = relationship()
    domain: Mapped[DropDomain | None] = relationship()
    worker: Mapped[WorkerNode | None] = relationship()
