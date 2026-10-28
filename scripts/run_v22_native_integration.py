"""Own disposable native PostgreSQL/Redis processes and run recovery acceptance.

Supply installed binaries explicitly; this runner never installs tools, loads
application credentials or connects to an existing database. All data lives in
one newly created temporary directory, which is removed after owned processes
have stopped. See requirements-native-tests.txt for the optional Python driver.
"""
import argparse
from contextlib import ExitStack
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]


def free_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def stop(process):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=8)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=8)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--postgres-bin", type=Path, required=True, help="Directory containing initdb and postgres")
    parser.add_argument("--redis-server", type=Path, required=True)
    parser.add_argument("--frontend-root", type=Path, default=ROOT.parent / "search-trust")
    parser.add_argument("pytest_args", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    import psycopg
    from psycopg import sql
    from redis import Redis

    for binary in (args.postgres_bin / "initdb", args.postgres_bin / "postgres", args.redis_server):
        if not binary.is_file() or not os.access(binary, os.X_OK):
            parser.error(f"Executable missing: {binary}")
    # The child gets only OS necessities and the test endpoints owned below.
    environment = {key: os.environ[key] for key in ("PATH", "PYTHONPATH", "LANG", "LC_ALL", "TMPDIR") if key in os.environ}
    environment.update(SEARCHTRUST_TESTING="1", PYTHONDONTWRITEBYTECODE="1", PYTHONUNBUFFERED="1")
    # This disposable loopback cluster has neither TLS nor Kerberos configured.
    os.environ.update(PGSSLMODE="disable", PGGSSENCMODE="disable")
    environment.update(PGSSLMODE="disable", PGGSSENCMODE="disable")
    with ExitStack() as stack:
        directory = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="searchtrust-native-run-")))
        log = stack.enter_context((directory / "services.log").open("wb"))
        subprocess.run([str(args.postgres_bin / "initdb"), "-D", str(directory / "pg"), "-U", "searchtrust_test",
            "--auth=trust", "--no-locale", "--encoding=UTF8"], env=environment, stdout=log, stderr=log, check=True, timeout=30)
        postgres_port, redis_port = free_port(), free_port()
        while redis_port==postgres_port:
            redis_port=free_port()
        postgres = subprocess.Popen([str(args.postgres_bin / "postgres"), "-D", str(directory / "pg"),
            "-h", "127.0.0.1", "-p", str(postgres_port), "-k", str(directory)], env=environment, stdout=log, stderr=log)
        stack.callback(stop, postgres)
        redis = subprocess.Popen([str(args.redis_server), "--bind", "127.0.0.1", "--port", str(redis_port),
            "--save", "", "--appendonly", "no", "--dir", str(directory)], env=environment, stdout=log, stderr=log)
        stack.callback(stop, redis)
        admin = f"postgresql://searchtrust_test@127.0.0.1:{postgres_port}/postgres"
        deadline = time.monotonic() + 20
        while True:
            try:
                with psycopg.connect(admin, connect_timeout=1):
                    pass
                with Redis(host="127.0.0.1", port=redis_port, db=15) as client:
                    client.ping()
                break
            except Exception as error:
                if postgres.poll() is not None or redis.poll() is not None or time.monotonic() >= deadline:
                    log.flush()
                    details=(directory / "services.log").read_text(errors="replace")[-3000:]
                    raise RuntimeError(f"Disposable native services did not become ready ({type(error).__name__}):\n{details}") from None
                time.sleep(0.05)
        database = "searchtrust_v22_test_" + uuid4().hex
        with psycopg.connect(admin, autocommit=True) as connection:
            connection.execute("create role anon; create role authenticated; create role service_role bypassrls")
            connection.execute(sql.SQL("create database {} ").format(sql.Identifier(database)))
        database_url = admin.rsplit("/", 1)[0] + "/" + database
        migrations = sorted((args.frontend_root / "supabase/migrations").glob("*.sql"))
        if not migrations:
            raise RuntimeError("No frontend migrations found")
        with psycopg.connect(database_url, autocommit=True) as connection:
            for migration in migrations:
                connection.execute(migration.read_text(encoding="utf-8"))
            version = connection.execute("show server_version").fetchone()[0]
        environment.update(V22_TEST_POSTGRES_URL=database_url,
            V22_TEST_REDIS_URL=f"redis://127.0.0.1:{redis_port}/15",
            V22_TEST_REDIS_PREFIX=f"searchtrust:v22:test:{uuid4().hex}:")
        print(f"Native acceptance: PostgreSQL {version}, {len(migrations)} migrations, disposable loopback Redis", flush=True)
        extra = args.pytest_args[1:] if args.pytest_args[:1] == ["--"] else args.pytest_args
        command = [sys.executable, "-m", "pytest", "-q", "-m", "redis_integration or native_integration",
            "--allow-hosts=127.0.0.1,localhost,::1", *extra]
        return subprocess.run(command, cwd=ROOT, env=environment, check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
