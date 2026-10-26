"""Run small forward-only database migrations for an existing deployment.

This project does not use Alembic.  Keep migrations explicit and idempotent so
an operator can run them before deploying a new application version.
"""

from app.database import engine
from app.models import LeadIdempotency


def main() -> None:
    # The table definition includes its foreign keys, unique constraint, and
    # expiry index.  checkfirst makes this safe to run repeatedly.
    LeadIdempotency.__table__.create(bind=engine, checkfirst=True)
    print("Migration 001 completed: lead_idempotency is ready.")


if __name__ == "__main__":
    main()
