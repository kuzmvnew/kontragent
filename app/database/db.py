import sqlite3
from pathlib import Path


DB_PATH = Path(__file__).resolve().parents[2] / "kontragent.db"


def get_connection():
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    return connection


def create_tables():
    connection = get_connection()

    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS companies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            inn TEXT UNIQUE NOT NULL,
            kpp TEXT,
            ogrn TEXT,
            name TEXT NOT NULL,
            short_name TEXT,
            full_name TEXT,
            address TEXT,
            director_name TEXT,
            director_position TEXT,
            okved TEXT,
            registration_date TEXT,
            status TEXT,
            risk_score INTEGER DEFAULT 0
        )
        """
    )

    existing_columns = {
        row["name"]
        for row in connection.execute("PRAGMA table_info(companies)").fetchall()
    }

    columns_to_add = [
        ("kpp", "TEXT"),
        ("short_name", "TEXT"),
        ("full_name", "TEXT"),
        ("address", "TEXT"),
        ("director_name", "TEXT"),
        ("director_position", "TEXT"),
        ("okved", "TEXT"),
        ("registration_date", "TEXT"),
    ]

    for column_name, column_type in columns_to_add:
        if column_name not in existing_columns:
            connection.execute(
                f"ALTER TABLE companies ADD COLUMN {column_name} {column_type}"
            )

    connection.commit()
    connection.close()