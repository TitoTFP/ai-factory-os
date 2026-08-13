"""Add provider usage accounting."""

from alembic import op
import sqlalchemy as sa

revision = "0002_usage"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "usage_records",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("factory_id", sa.String(36), sa.ForeignKey("factories.id", ondelete="CASCADE"), nullable=False),
        sa.Column("agent_id", sa.String(36), sa.ForeignKey("agents.id", ondelete="SET NULL")),
        sa.Column("task_id", sa.String(36), sa.ForeignKey("tasks.id", ondelete="SET NULL")),
        sa.Column("provider", sa.String(64), nullable=False),
        sa.Column("model", sa.String(160), nullable=False),
        sa.Column("request_kind", sa.String(64), nullable=False),
        sa.Column("prompt_tokens", sa.Integer, nullable=False),
        sa.Column("completion_tokens", sa.Integer, nullable=False),
        sa.Column("total_tokens", sa.Integer, nullable=False),
        sa.Column("cost_usd", sa.Float, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_usage_records_factory_id", "usage_records", ["factory_id"])
    op.create_index("ix_usage_records_agent_id", "usage_records", ["agent_id"])
    op.create_index("ix_usage_records_task_id", "usage_records", ["task_id"])
    op.create_index("ix_usage_records_created_at", "usage_records", ["created_at"])
    op.add_column("goals", sa.Column("evaluation", sa.JSON(), nullable=False, server_default=sa.text("'{}'")))
    op.add_column("messages", sa.Column("correlation_id", sa.String(128), nullable=True))
    op.add_column("messages", sa.Column("idempotency_key", sa.String(128), nullable=True))
    op.add_column("messages", sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("messages", sa.Column("read_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_messages_correlation_id", "messages", ["correlation_id"])
    op.create_index("ix_messages_status", "messages", ["status"])
    with op.batch_alter_table("messages") as batch:
        batch.create_unique_constraint("uq_factory_message_idempotency", ["factory_id", "idempotency_key"])


def downgrade() -> None:
    with op.batch_alter_table("messages") as batch:
        batch.drop_constraint("uq_factory_message_idempotency", type_="unique")
    op.drop_index("ix_messages_status", table_name="messages")
    op.drop_index("ix_messages_correlation_id", table_name="messages")
    op.drop_column("messages", "read_at")
    op.drop_column("messages", "delivered_at")
    op.drop_column("messages", "idempotency_key")
    op.drop_column("messages", "correlation_id")
    op.drop_column("goals", "evaluation")
    op.drop_index("ix_usage_records_created_at", table_name="usage_records")
    op.drop_index("ix_usage_records_task_id", table_name="usage_records")
    op.drop_index("ix_usage_records_agent_id", table_name="usage_records")
    op.drop_index("ix_usage_records_factory_id", table_name="usage_records")
    op.drop_table("usage_records")
