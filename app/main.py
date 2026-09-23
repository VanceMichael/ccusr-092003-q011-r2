
import os
from http.server import ThreadingHTTPServer

from app.database import connect, migrate
from app.server import build_handler
from app.store import Store


def main() -> None:
    database_path = os.getenv("DATABASE_PATH", "data/app.sqlite3")
    connection = connect(database_path)
    migrate(connection)
    store = Store(connection)
    port = int(os.getenv("PORT", "8080"))
    server = ThreadingHTTPServer(("0.0.0.0", port), build_handler(store))
    print(f"验收平台已启动：0.0.0.0:{port}（数据库 {database_path}）")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        connection.close()


if __name__ == "__main__":
    main()
