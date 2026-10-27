from app.db.migrations.runner import MIGRATIONS_DIR, compute_checksum


def test_migrations_directory_exists_and_has_files():
    assert MIGRATIONS_DIR.exists()

    sql_files = list(MIGRATIONS_DIR.glob("*.sql"))

    assert len(sql_files) >= 1
    assert any(f.name.startswith("0001_") for f in sql_files)


def test_compute_checksum():
    c1 = compute_checksum("SELECT 1;")
    c2 = compute_checksum("SELECT 1;")
    c3 = compute_checksum("SELECT 2;")

    assert c1 == c2
    assert c1 != c3
    assert len(c1) == 64


def test_migration_files_content_valid():
    for f in MIGRATIONS_DIR.glob("*.sql"):
        text = f.read_text(encoding="utf-8")

        assert len(text.strip()) > 0
        assert any(statement in text.upper() for statement in ("CREATE TABLE", "ALTER TABLE"))
