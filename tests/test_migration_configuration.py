from pathlib import Path


def test_migrations_honor_service_database_url() -> None:
    migration_environment = Path("migrations/env.py").read_text(encoding="utf-8")
    assert 'os.getenv("ENGINEERING_DATABASE_URL")' in migration_environment
    assert 'config.set_main_option("sqlalchemy.url"' in migration_environment
