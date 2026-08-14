"""factory zero repository and improvement cycles

Revision ID: 0003_factory_zero
Revises: 0002_usage
"""

from alembic import op
import sqlalchemy as sa


revision = "0003_factory_zero"
down_revision = "0002_usage"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "repositories",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("factory_id", sa.String(36), sa.ForeignKey("factories.id", ondelete="CASCADE"), nullable=False),
        sa.Column("provider", sa.String(32), nullable=False, server_default="github"),
        sa.Column("owner", sa.String(160), nullable=False),
        sa.Column("name", sa.String(160), nullable=False),
        sa.Column("remote_url", sa.String(1024), nullable=False),
        sa.Column("default_branch", sa.String(160), nullable=False, server_default="master"),
        sa.Column("test_commands", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("build_commands", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("lint_commands", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("status", sa.String(24), nullable=False, server_default="active"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("factory_id", "provider", "owner", "name", name="uq_factory_repository"),
    )
    op.create_index("ix_repositories_factory_id", "repositories", ["factory_id"])
    op.create_index("ix_repositories_status", "repositories", ["status"])

    op.create_table(
        "repository_credentials",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("factory_id", sa.String(36), sa.ForeignKey("factories.id", ondelete="CASCADE"), nullable=False),
        sa.Column("repository_id", sa.String(36), sa.ForeignKey("repositories.id", ondelete="CASCADE"), nullable=False),
        sa.Column("provider", sa.String(32), nullable=False, server_default="github"),
        sa.Column("encrypted_token", sa.Text(), nullable=False),
        sa.Column("permissions", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("repository_id", "provider", name="uq_repository_credential_provider"),
    )
    op.create_index("ix_repository_credentials_factory_id", "repository_credentials", ["factory_id"])
    op.create_index("ix_repository_credentials_repository_id", "repository_credentials", ["repository_id"])

    op.create_table(
        "improvement_cycles",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("factory_id", sa.String(36), sa.ForeignKey("factories.id", ondelete="CASCADE"), nullable=False),
        sa.Column("repository_id", sa.String(36), sa.ForeignKey("repositories.id", ondelete="CASCADE"), nullable=False),
        sa.Column("status", sa.String(24), nullable=False, server_default="queued"),
        sa.Column("phase", sa.String(32), nullable=False, server_default="discover"),
        sa.Column("objective", sa.Text(), nullable=False),
        sa.Column("branch_name", sa.String(240), nullable=True),
        sa.Column("worktree_path", sa.String(1024), nullable=True),
        sa.Column("base_sha", sa.String(64), nullable=True),
        sa.Column("head_sha", sa.String(64), nullable=True),
        sa.Column("pr_number", sa.Integer(), nullable=True),
        sa.Column("pr_url", sa.String(1024), nullable=True),
        sa.Column("author_agent_id", sa.String(36), sa.ForeignKey("agents.id", ondelete="SET NULL"), nullable=True),
        sa.Column("reviewer_agent_id", sa.String(36), sa.ForeignKey("agents.id", ondelete="SET NULL"), nullable=True),
        sa.Column("proposal", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("verification", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("review", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("observation", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_improvement_cycles_factory_id", "improvement_cycles", ["factory_id"])
    op.create_index("ix_improvement_cycles_repository_id", "improvement_cycles", ["repository_id"])
    op.create_index("ix_improvement_cycles_status", "improvement_cycles", ["status"])
    op.create_index("ix_improvement_cycles_phase", "improvement_cycles", ["phase"])
    op.create_index("ix_improvement_cycles_author_agent_id", "improvement_cycles", ["author_agent_id"])
    op.create_index("ix_improvement_cycles_reviewer_agent_id", "improvement_cycles", ["reviewer_agent_id"])
    op.create_index("ix_improvement_cycles_lease_until", "improvement_cycles", ["lease_until"])
    op.create_index("ix_improvement_cycles_created_at", "improvement_cycles", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_improvement_cycles_created_at", table_name="improvement_cycles")
    op.drop_index("ix_improvement_cycles_lease_until", table_name="improvement_cycles")
    op.drop_index("ix_improvement_cycles_reviewer_agent_id", table_name="improvement_cycles")
    op.drop_index("ix_improvement_cycles_author_agent_id", table_name="improvement_cycles")
    op.drop_index("ix_improvement_cycles_phase", table_name="improvement_cycles")
    op.drop_index("ix_improvement_cycles_status", table_name="improvement_cycles")
    op.drop_index("ix_improvement_cycles_repository_id", table_name="improvement_cycles")
    op.drop_index("ix_improvement_cycles_factory_id", table_name="improvement_cycles")
    op.drop_table("improvement_cycles")
    op.drop_index("ix_repository_credentials_repository_id", table_name="repository_credentials")
    op.drop_index("ix_repository_credentials_factory_id", table_name="repository_credentials")
    op.drop_table("repository_credentials")
    op.drop_index("ix_repositories_status", table_name="repositories")
    op.drop_index("ix_repositories_factory_id", table_name="repositories")
    op.drop_table("repositories")
