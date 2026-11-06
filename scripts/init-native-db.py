"""One-time native fallback setup. Run as root/sudo on a local Ubuntu PostgreSQL host."""
import os
import subprocess
from pathlib import Path

from psycopg import sql

root = Path(__file__).resolve().parents[1]
if os.geteuid() != 0:
    raise SystemExit("请通过 sudo 运行；或自行创建数据库并设置 DATABASE_URL")
password = (root / "secrets/postgres_password").read_text().strip()
# Feed SQL through stdin so the password never appears in a process argument or log.
role = "animation_local"
statement = sql.SQL("""
DO $body$ BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'animation_local') THEN
    CREATE ROLE animation_local LOGIN;
  END IF;
END $body$;
ALTER ROLE animation_local PASSWORD {};
""").format(sql.Literal(password)).as_string()
subprocess.run(["runuser", "-u", "postgres", "--", "psql", "-q", "-v", "ON_ERROR_STOP=1"],
               input=statement, text=True, check=True)
for name in ("animation_local", "animation_test"):
    result = subprocess.run(["runuser", "-u", "postgres", "--", "psql", "-At", "-c",
        f"SELECT 1 FROM pg_database WHERE datname = '{name}'"], capture_output=True, text=True, check=True)
    if result.stdout.strip() != "1":
        subprocess.run(["runuser", "-u", "postgres", "--", "createdb", "-O", role, name], check=True)
print("本地数据库 animation_local 和隔离验证库 animation_test 已准备。")
