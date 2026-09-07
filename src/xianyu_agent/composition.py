"""Process-level composition root for application services."""

from xianyu_agent.application.accounts import AccountApplication
from xianyu_agent.infrastructure.database.unit_of_work import SqlAlchemyUnitOfWork

_ACCOUNT_APPLICATION = AccountApplication(SqlAlchemyUnitOfWork)


def get_account_application() -> AccountApplication:
    """Return the process-wide stateless account application service."""
    return _ACCOUNT_APPLICATION
