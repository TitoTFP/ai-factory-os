"""Initial AI Factory OS schema."""

from alembic import op
import sqlalchemy as sa

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("email", sa.String(320), nullable=False, unique=True),
        sa.Column("name", sa.String(160), nullable=False, server_default=""),
        sa.Column("password_hash", sa.String(256)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "oauth_states",
        sa.Column("id", sa.String(128), primary_key=True),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "oauth_identities",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("subject", sa.String(512), nullable=False),
        sa.Column("email", sa.String(320), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("provider", "subject", name="uq_oauth_provider_subject"),
    )
    op.create_table(
        "factories",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("owner_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(160), nullable=False),
        sa.Column("mission", sa.Text, nullable=False),
        sa.Column("primary_objective", sa.Text, nullable=False),
        sa.Column("constraints", sa.JSON, nullable=False),
        sa.Column("autonomy", sa.String(32), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "factory_credentials",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("factory_id", sa.String(36), sa.ForeignKey("factories.id", ondelete="CASCADE"), nullable=False),
        sa.Column("provider", sa.String(64), nullable=False),
        sa.Column("base_url", sa.String(1024), nullable=False),
        sa.Column("model", sa.String(160), nullable=False),
        sa.Column("encrypted_api_key", sa.Text, nullable=False),
        sa.Column("permissions", sa.JSON, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("factory_id", "provider", name="uq_factory_credential_provider"),
    )
    op.create_table(
        "spaces",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("factory_id", sa.String(36), sa.ForeignKey("factories.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(160), nullable=False),
        sa.Column("purpose", sa.Text, nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("shared_memory", sa.JSON, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "agents",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("factory_id", sa.String(36), sa.ForeignKey("factories.id", ondelete="CASCADE"), nullable=False),
        sa.Column("space_id", sa.String(36), sa.ForeignKey("spaces.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(160), nullable=False),
        sa.Column("role", sa.String(160), nullable=False),
        sa.Column("objective", sa.Text, nullable=False),
        sa.Column("responsibilities", sa.JSON, nullable=False),
        sa.Column("model", sa.String(160), nullable=False),
        sa.Column("system_prompt", sa.Text, nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("current_task_id", sa.String(36)),
        sa.Column("budget", sa.JSON, nullable=False),
        sa.Column("relationships", sa.JSON, nullable=False),
        sa.Column("private_memory", sa.JSON, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "goals",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("factory_id", sa.String(36), sa.ForeignKey("factories.id", ondelete="CASCADE"), nullable=False),
        sa.Column("parent_id", sa.String(36), sa.ForeignKey("goals.id", ondelete="SET NULL")),
        sa.Column("title", sa.String(240), nullable=False),
        sa.Column("objective", sa.Text, nullable=False),
        sa.Column("criteria", sa.JSON, nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("completion_note", sa.Text, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
    )
    op.create_table(
        "tasks",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("factory_id", sa.String(36), sa.ForeignKey("factories.id", ondelete="CASCADE"), nullable=False),
        sa.Column("goal_id", sa.String(36), sa.ForeignKey("goals.id", ondelete="SET NULL")),
        sa.Column("parent_id", sa.String(36), sa.ForeignKey("tasks.id", ondelete="SET NULL")),
        sa.Column("assignee_id", sa.String(36), sa.ForeignKey("agents.id", ondelete="SET NULL")),
        sa.Column("title", sa.String(240), nullable=False),
        sa.Column("description", sa.Text, nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("inputs", sa.JSON, nullable=False),
        sa.Column("outputs", sa.JSON, nullable=False),
        sa.Column("retry_count", sa.Integer, nullable=False),
        sa.Column("max_retries", sa.Integer, nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_until", sa.DateTime(timezone=True)),
        sa.Column("error", sa.Text, nullable=False),
        sa.Column("created_by", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "messages",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("factory_id", sa.String(36), sa.ForeignKey("factories.id", ondelete="CASCADE"), nullable=False),
        sa.Column("sender_agent_id", sa.String(36), sa.ForeignKey("agents.id", ondelete="SET NULL")),
        sa.Column("recipient_agent_id", sa.String(36), sa.ForeignKey("agents.id", ondelete="SET NULL")),
        sa.Column("message_type", sa.String(32), nullable=False),
        sa.Column("subject", sa.String(240), nullable=False),
        sa.Column("body", sa.Text, nullable=False),
        sa.Column("payload", sa.JSON, nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "artifacts",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("factory_id", sa.String(36), sa.ForeignKey("factories.id", ondelete="CASCADE"), nullable=False),
        sa.Column("space_id", sa.String(36), sa.ForeignKey("spaces.id", ondelete="SET NULL")),
        sa.Column("agent_id", sa.String(36), sa.ForeignKey("agents.id", ondelete="SET NULL")),
        sa.Column("task_id", sa.String(36), sa.ForeignKey("tasks.id", ondelete="SET NULL")),
        sa.Column("name", sa.String(240), nullable=False),
        sa.Column("kind", sa.String(64), nullable=False),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("uri", sa.String(1024), nullable=False),
        sa.Column("metadata", sa.JSON, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "tools",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("factory_id", sa.String(36), sa.ForeignKey("factories.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(80), nullable=False),
        sa.Column("description", sa.Text, nullable=False),
        sa.Column("enabled", sa.Boolean, nullable=False),
        sa.Column("permissions", sa.JSON, nullable=False),
        sa.Column("config_json", sa.JSON, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("factory_id", "name", name="uq_factory_tool_name"),
    )
    op.create_table(
        "events",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("factory_id", sa.String(36), sa.ForeignKey("factories.id", ondelete="CASCADE"), nullable=False),
        sa.Column("actor_type", sa.String(32), nullable=False),
        sa.Column("actor_id", sa.String(36), nullable=False),
        sa.Column("event_type", sa.String(80), nullable=False),
        sa.Column("payload", sa.JSON, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "factory_runs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("factory_id", sa.String(36), sa.ForeignKey("factories.id", ondelete="CASCADE"), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("stopped_at", sa.DateTime(timezone=True)),
        sa.Column("last_error", sa.Text, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    for table in [
        "factory_runs", "events", "tools", "artifacts", "messages", "tasks", "goals",
        "agents", "spaces", "factory_credentials", "factories", "oauth_identities",
        "oauth_states", "users",
    ]:
        op.drop_table(table)
