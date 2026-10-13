import os

from alembic import context
from sqlalchemy import create_engine

from max_postiz_agent.db import Base
from max_postiz_agent import hub_models  # noqa: F401

url = os.environ.get("DATABASE_URL")
connection = context.config.attributes.get("connection")
if not url and connection is None:
    raise RuntimeError("DATABASE_URL is required; migrations never fall back to SQLite")
if context.is_offline_mode():
    context.configure(url=url, target_metadata=Base.metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()
else:
    if connection is not None:
        context.configure(connection=connection, target_metadata=Base.metadata)
        with context.begin_transaction():
            context.run_migrations()
    else:
        engine = create_engine(url)
        with engine.connect() as conn:
            context.configure(connection=conn, target_metadata=Base.metadata)
            with context.begin_transaction():
                context.run_migrations()
        engine.dispose()
