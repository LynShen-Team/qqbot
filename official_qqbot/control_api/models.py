from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    user_key = Column(String(128), nullable=False, unique=True, index=True)
    display_name = Column(String(255), nullable=True)
    last_group_openid = Column(String(128), nullable=True)
    is_banned = Column(Boolean, nullable=False, default=False)
    banned_reason = Column(Text, nullable=True)
    first_seen_at = Column(DateTime(timezone=True), nullable=False, default=utc_now)
    last_seen_at = Column(DateTime(timezone=True), nullable=False, default=utc_now)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=utc_now)


class EmailBinding(Base):
    __tablename__ = "email_bindings"

    id = Column(Integer, primary_key=True)
    user_key = Column(String(128), nullable=False, unique=True, index=True)
    email = Column(String(255), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utc_now)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=utc_now)


class Role(Base):
    __tablename__ = "roles"
    __table_args__ = (UniqueConstraint("user_key", "role", name="uq_roles_user_role"),)

    id = Column(Integer, primary_key=True)
    user_key = Column(String(128), nullable=False, index=True)
    role = Column(String(32), nullable=False, index=True)
    password = Column(String(255), nullable=False, default="")
    added_by = Column(String(128), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utc_now)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=utc_now)


class ResourceLimitRule(Base):
    __tablename__ = "resource_limit_rules"

    id = Column(Integer, primary_key=True)
    resource = Column(String(32), nullable=False, unique=True, index=True)
    limit_count = Column(Integer, nullable=False)
    window_unit = Column(String(32), nullable=False)
    window_seconds = Column(Integer, nullable=False)
    updated_by = Column(String(128), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utc_now)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=utc_now)


class ResourceUsage(Base):
    __tablename__ = "resource_usage"

    id = Column(Integer, primary_key=True)
    resource = Column(String(32), nullable=False, index=True)
    user_key = Column(String(128), nullable=False, index=True)
    used_at = Column(DateTime(timezone=True), nullable=False, default=utc_now, index=True)
    source_group_openid = Column(String(128), nullable=True)
    message_id = Column(Text, nullable=True)  # QQ ids up to 137 chars, some outbound ids longer


class CommandLog(Base):
    __tablename__ = "command_logs"

    id = Column(Integer, primary_key=True)
    user_key = Column(String(128), nullable=True, index=True)
    group_openid = Column(String(128), nullable=True, index=True)
    command = Column(String(128), nullable=True)
    content = Column(Text, nullable=True)
    message_id = Column(Text, nullable=True)  # QQ ids up to 137 chars, some outbound ids longer
    created_at = Column(DateTime(timezone=True), nullable=False, default=utc_now, index=True)


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id = Column(Integer, primary_key=True)
    actor_user_key = Column(String(128), nullable=True, index=True)
    action = Column(String(128), nullable=False)
    target_user_key = Column(String(128), nullable=True, index=True)
    detail = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utc_now, index=True)


class OutboundLog(Base):
    __tablename__ = "outbound_logs"

    id = Column(Integer, primary_key=True)
    user_key = Column(String(128), nullable=True, index=True)
    group_openid = Column(String(128), nullable=True, index=True)
    channel = Column(String(64), nullable=True)
    status = Column(String(64), nullable=True)
    content = Column(Text, nullable=True)
    message_id = Column(Text, nullable=True)  # QQ ids up to 137 chars, some outbound ids longer
    created_at = Column(DateTime(timezone=True), nullable=False, default=utc_now, index=True)


class Announcement(Base):
    __tablename__ = "announcements"

    id = Column(Integer, primary_key=True)
    source = Column(String(128), nullable=False, default="Miracle")
    title = Column(String(255), nullable=False)
    body = Column(Text, nullable=False)
    link_text = Column(String(128), nullable=False, default="")
    link_url = Column(String(512), nullable=False, default="")
    is_public = Column(Boolean, nullable=False, default=True)
    created_by = Column(String(128), nullable=False, default="dashboard")
    created_at = Column(DateTime(timezone=True), nullable=False, default=utc_now, index=True)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=utc_now)


class UpdatePackage(Base):
    __tablename__ = "update_packages"

    id = Column(Integer, primary_key=True)
    version = Column(String(128), nullable=False)
    title = Column(String(255), nullable=False)
    body = Column(Text, nullable=False)
    is_public = Column(Boolean, nullable=False, default=True)
    created_by = Column(String(128), nullable=False, default="dashboard")
    created_at = Column(DateTime(timezone=True), nullable=False, default=utc_now, index=True)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=utc_now)


# ---------------------------------------------------------------------------
# Bot state that used to live in data/*.json (points, identities, ads, cooldowns).
# Used when BOT_STATE_STORE=db; the JSON files stay the format for BOT_STATE_STORE=json.
# ---------------------------------------------------------------------------


class PointsAccount(Base):
    """One points account (sign_official.json entry). account_key: "QQ<number>", a union id or an openid."""

    __tablename__ = "points_accounts"

    account_key = Column(String(160), primary_key=True)
    points = Column(Integer, nullable=False, default=0)
    streak = Column(Integer, nullable=False, default=0)
    last_sign = Column(String(16), nullable=False, default="")
    group_add_last = Column(String(16), nullable=True)
    group_add_count = Column(Integer, nullable=True)
    rewarded_groups = Column(Text, nullable=True)  # JSON list of group openids
    merged_into = Column(String(160), nullable=True, index=True)
    extra = Column(Text, nullable=True)  # JSON object with any other keys, kept verbatim
    updated_at = Column(DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now)


class IdentityLink(Base):
    """scenario openid -> union openid (identity_map.json plain entries)."""

    __tablename__ = "identity_links"

    scenario_openid = Column(String(160), primary_key=True)
    union_openid = Column(String(160), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utc_now)


class QQBind(Base):
    """limit_user_id -> QQ number (qq_binds.json)."""

    __tablename__ = "qq_binds"

    limit_user_id = Column(String(160), primary_key=True)
    qq = Column(String(16), nullable=False, index=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utc_now)


class PendingGroupReward(Base):
    """identity_map.json "_pending": invite rewards waiting for the inviter to speak in the group."""

    __tablename__ = "pending_group_rewards"

    id = Column(Integer, primary_key=True)
    group_openid = Column(String(160), nullable=False, index=True)
    member_openid = Column(String(160), nullable=False)
    points = Column(Integer, nullable=False, default=0)
    ts = Column(Integer, nullable=False, default=0)
    notified = Column(Boolean, nullable=False, default=False)


class BotMention(Base):
    """identity_map.json "_bot_mentions": the bot's learned @id per group."""

    __tablename__ = "bot_mentions"

    group_openid = Column(String(160), primary_key=True)
    mention_id = Column(String(64), nullable=False, index=True)


class BotAd(Base):
    """ads.json. active_until is a unix timestamp (float) like in the JSON file."""

    __tablename__ = "bot_ads"

    id = Column(Integer, primary_key=True, autoincrement=False)
    content = Column(Text, nullable=False)
    enabled = Column(Boolean, nullable=False, default=False)
    active_until = Column(Float, nullable=True)
    created_at = Column(String(32), nullable=True)


class CooldownEntry(Base):
    """cooldowns.json flattened: (section, key) -> JSON value.

    section is e.g. "163_cd", "sauth_rate", "nfa_ban", "restrict_rules"; the nested
    "restrict_log" becomes one section per feature: "restrict_log:<feature>".
    """

    __tablename__ = "cooldown_entries"

    section = Column(String(64), primary_key=True)
    key = Column(String(160), primary_key=True)
    value = Column(Text, nullable=False)
