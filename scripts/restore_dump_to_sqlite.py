"""Script to restore PostgreSQL dump (.sql.gz) into local SQLite database."""
import gzip
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db_sqlite_schema import ensure_sqlite_schema, setup_sqlite_functions
from database_manager import _sqlite_path


def restore_dump(dump_path: str, db_path: str | None = None) -> dict[str, int]:
    target_db = db_path or _sqlite_path()
    os.makedirs(os.path.dirname(os.path.abspath(target_db)), exist_ok=True)

    print(f"Restoring dump {dump_path} -> {target_db}...")
    ensure_sqlite_schema(target_db)

    con = sqlite3.connect(target_db)
    setup_sqlite_functions(con, outbox_enabled=False)
    cur = con.cursor()

    current_table = None
    columns = []
    inserted_counts: dict[str, int] = {}

    with gzip.open(dump_path, "rt", encoding="utf-8", errors="ignore") as f:
        for line in f:
            if line.startswith("COPY public."):
                parts = line.split()
                current_table = parts[1].replace("public.", "")
                cols_str = line[line.find("(") + 1 : line.find(")")]
                columns = [c.strip() for c in cols_str.split(",")]
                inserted_counts[current_table] = 0
            elif current_table:
                if line.strip() == r"\.":
                    current_table = None
                else:
                    vals = line.rstrip("\n").split("\t")
                    row = [None if v == r"\N" else v for v in vals]
                    placeholders = ", ".join(["?"] * len(columns))
                    col_names = ", ".join(columns)
                    sql = f"INSERT OR REPLACE INTO {current_table} ({col_names}) VALUES ({placeholders})"
                    cur.execute(sql, row)
                    inserted_counts[current_table] += 1

    cur.execute("INSERT OR REPLACE INTO database_metadata (key, value) VALUES ('postgres_snapshot_complete', '1')")
    cur.execute("INSERT OR REPLACE INTO database_metadata (key, value) VALUES ('pg_outbox_cursor', '0')")
    con.commit()
    con.close()

    print("Restored records:", inserted_counts)
    return inserted_counts


if __name__ == "__main__":
    default_dump = "/workspace/.polydev/attachments/9d2b33be_pubg-db-20261108-1208.sql.gz"
    path = sys.argv[1] if len(sys.argv) > 1 else default_dump
    if not os.path.isfile(path):
        print(f"Dump file not found: {path}")
        sys.exit(1)
    restore_dump(path)
