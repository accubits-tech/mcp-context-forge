# -*- coding: utf-8 -*-
"""add_events_tables

Revision ID: e6v7t8b9tbl0
Revises: e1v2t3a4col5
Create Date: 2026-05-30 00:00:00.000000
"""

# Standard
from typing import Sequence, Union

# Third-Party
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "e6v7t8b9tbl0"
down_revision: Union[str, Sequence[str], None] = "e1v2t3a4col5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create the event subscription, log, delivery, and dead-letter tables."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if not inspector.has_table("event_subscriptions"):
        op.create_table(
            "event_subscriptions",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("gateway_id", sa.String(36), sa.ForeignKey("gateways.id", ondelete="CASCADE"), nullable=True),
            sa.Column("team_id", sa.String(36), sa.ForeignKey("email_teams.id", ondelete="SET NULL"), nullable=True),
            sa.Column("owner_email", sa.String(255), nullable=True),
            sa.Column("subscriber_kind", sa.String(20), nullable=False),
            sa.Column("callback_url", sa.String(2048), nullable=True),
            sa.Column("subscriber_target_ref", sa.String(767), nullable=True),
            sa.Column("target", sa.JSON(), nullable=True),
            sa.Column("source", sa.String(255), nullable=True),
            sa.Column("event_types", sa.JSON(), nullable=False),
            sa.Column("filter_expr", sa.Text(), nullable=True),
            sa.Column("mode", sa.String(12), nullable=False, server_default="fanout"),
            sa.Column("correlation_key", sa.String(512), nullable=True),
            sa.Column("correlation_value", sa.String(512), nullable=True),
            sa.Column("delivery", sa.JSON(), nullable=True),
            sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index("ix_event_subscriptions_gateway_id", "event_subscriptions", ["gateway_id"])
        op.create_index("ix_event_subscriptions_team_id", "event_subscriptions", ["team_id"])
        op.create_index("ix_event_subs_tenant_source_active", "event_subscriptions", ["team_id", "source", "active"])
        op.create_index("ix_event_subscriptions_gw_mode", "event_subscriptions", ["gateway_id", "mode"])
        op.create_index("ix_event_subscriptions_corr_value", "event_subscriptions", ["correlation_value"])
        print("Created event_subscriptions table.")
    else:
        print("event_subscriptions already exists; skipping create.")

    if not inspector.has_table("event_log"):
        op.create_table(
            "event_log",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("evt_id", sa.String(255), nullable=False),
            sa.Column("evt_source", sa.String(767), nullable=False),
            sa.Column("evt_type", sa.String(255), nullable=False),
            sa.Column("evt_subject", sa.String(512), nullable=True),
            sa.Column("evt_time", sa.DateTime(timezone=True), nullable=True),
            sa.Column("gateway_id", sa.String(36), sa.ForeignKey("gateways.id", ondelete="CASCADE"), nullable=True),
            sa.Column("provider_id", sa.String(128), nullable=True),
            sa.Column("data", sa.JSON(), nullable=True),
            sa.Column("raw_headers", sa.JSON(), nullable=True),
            sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("evt_source", "evt_id", name="uq_event_log_source_id"),
        )
        op.create_index("ix_event_log_gateway_id", "event_log", ["gateway_id"])
        op.create_index("ix_event_log_gw_type_time", "event_log", ["gateway_id", "evt_type", "received_at"])
        print("Created event_log table.")
    else:
        print("event_log already exists; skipping create.")

    if not inspector.has_table("delivery_attempts"):
        op.create_table(
            "delivery_attempts",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("event_id", sa.String(36), sa.ForeignKey("event_log.id", ondelete="CASCADE"), nullable=False),
            sa.Column("subscription_id", sa.String(36), sa.ForeignKey("event_subscriptions.id", ondelete="CASCADE"), nullable=False),
            sa.Column("attempt_no", sa.Integer(), nullable=False),
            sa.Column("status", sa.String(16), nullable=False),
            sa.Column("http_status", sa.Integer(), nullable=True),
            sa.Column("idempotency_key", sa.String(255), nullable=False),
            sa.Column("error", sa.Text(), nullable=True),
            sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index("ix_delivery_attempts_event_id", "delivery_attempts", ["event_id"])
        op.create_index("ix_delivery_attempts_subscription_id", "delivery_attempts", ["subscription_id"])
        op.create_index("ix_delivery_attempts_event_sub", "delivery_attempts", ["event_id", "subscription_id"])
        op.create_index("ix_delivery_attempts_retry", "delivery_attempts", ["status", "next_retry_at"])
        print("Created delivery_attempts table.")
    else:
        print("delivery_attempts already exists; skipping create.")

    if not inspector.has_table("dead_letters"):
        op.create_table(
            "dead_letters",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("event_id", sa.String(36), sa.ForeignKey("event_log.id", ondelete="CASCADE"), nullable=False),
            sa.Column("subscription_id", sa.String(36), sa.ForeignKey("event_subscriptions.id", ondelete="SET NULL"), nullable=True),
            sa.Column("attempts", sa.Integer(), nullable=False),
            sa.Column("last_error", sa.Text(), nullable=True),
            sa.Column("payload_snapshot", sa.JSON(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index("ix_dead_letters_event_id", "dead_letters", ["event_id"])
        op.create_index("ix_dead_letters_subscription_id", "dead_letters", ["subscription_id"])
        print("Created dead_letters table.")
    else:
        print("dead_letters already exists; skipping create.")


def downgrade() -> None:
    """Drop the event tables in reverse foreign-key order."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if inspector.has_table("dead_letters"):
        existing_indexes = {idx["name"] for idx in inspector.get_indexes("dead_letters")}
        for idx_name in ("ix_dead_letters_subscription_id", "ix_dead_letters_event_id"):
            if idx_name in existing_indexes:
                op.drop_index(idx_name, table_name="dead_letters")
        op.drop_table("dead_letters")
        print("Dropped dead_letters table.")

    if inspector.has_table("delivery_attempts"):
        existing_indexes = {idx["name"] for idx in inspector.get_indexes("delivery_attempts")}
        for idx_name in (
            "ix_delivery_attempts_retry",
            "ix_delivery_attempts_event_sub",
            "ix_delivery_attempts_subscription_id",
            "ix_delivery_attempts_event_id",
        ):
            if idx_name in existing_indexes:
                op.drop_index(idx_name, table_name="delivery_attempts")
        op.drop_table("delivery_attempts")
        print("Dropped delivery_attempts table.")

    if inspector.has_table("event_log"):
        existing_indexes = {idx["name"] for idx in inspector.get_indexes("event_log")}
        for idx_name in ("ix_event_log_gw_type_time", "ix_event_log_gateway_id"):
            if idx_name in existing_indexes:
                op.drop_index(idx_name, table_name="event_log")
        op.drop_table("event_log")
        print("Dropped event_log table.")

    if inspector.has_table("event_subscriptions"):
        existing_indexes = {idx["name"] for idx in inspector.get_indexes("event_subscriptions")}
        for idx_name in (
            "ix_event_subscriptions_corr_value",
            "ix_event_subscriptions_gw_mode",
            "ix_event_subs_tenant_source_active",
            "ix_event_subscriptions_team_id",
            "ix_event_subscriptions_gateway_id",
        ):
            if idx_name in existing_indexes:
                op.drop_index(idx_name, table_name="event_subscriptions")
        op.drop_table("event_subscriptions")
        print("Dropped event_subscriptions table.")
