from app.database.db import create_tables, get_connection


create_tables()

connection = get_connection()

connection.execute(
    """
    INSERT OR IGNORE INTO companies (
        inn,
        ogrn,
        name,
        status,
        risk_score
    )
    VALUES (?, ?, ?, ?, ?)
    """,
    (
        "7707083893",
        "1027700132195",
        "ПАО СБЕРБАНК",
        "active",
        10
    )
)

connection.commit()
connection.close()

print("База данных создана")