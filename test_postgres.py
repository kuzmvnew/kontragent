import os

from dotenv import load_dotenv
from sqlalchemy import create_engine, text


load_dotenv()


database_url = os.getenv("DATABASE_URL")


if not database_url:
    raise RuntimeError(
        "В файле .env не найден DATABASE_URL"
    )


engine = create_engine(
    database_url,
    echo=False,
)


try:
    with engine.connect() as connection:

        result = connection.execute(
            text(
                """
                SELECT
                    current_database(),
                    current_user,
                    version()
                """
            )
        )

        row = result.fetchone()

        print()
        print("======================================")
        print("POSTGRESQL РАБОТАЕТ")
        print("======================================")
        print()
        print(f"База данных: {row[0]}")
        print(f"Пользователь: {row[1]}")
        print()
        print("Версия PostgreSQL:")
        print(row[2])
        print()
        print("Соединение успешно!")
        print()

except Exception as error:

    print()
    print("======================================")
    print("ОШИБКА ПОДКЛЮЧЕНИЯ")
    print("======================================")
    print()
    print(error)
    print()