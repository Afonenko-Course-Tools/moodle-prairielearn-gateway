from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
import sqlite3
from types import SimpleNamespace
from collections.abc import Mapping
from typing import NoReturn, Protocol


class Parameters(Protocol):
    def __len__(self) -> int: ...
    def __getitem__(self, key: int, /) -> object: ...

import pytest
import gateway.store as module
from gateway.store import Store

@pytest.mark.parametrize("code",[sqlite3.SQLITE_BUSY,sqlite3.SQLITE_LOCKED])
def test_initial_wal_lock_is_retried_on_same_connection(tmp_path: Path,monkeypatch: pytest.MonkeyPatch,code: int) -> None:
    original=sqlite3.connect;attempts: list[object] = []
    class Connection(sqlite3.Connection):
        def execute(self, sql: str, parameters: Parameters | Mapping[str, object] = (), /) -> sqlite3.Cursor:
            if sql=="PRAGMA journal_mode=WAL":
                attempts.append(self)
                if len(attempts)==1:
                    error=sqlite3.OperationalError("database is locked");error.sqlite_errorcode=code
                    raise error
            return super().execute(sql,parameters)
    def connect(database: str | Path, *, timeout: float = 5, uri: bool = False) -> Connection:
        return original(database, timeout=timeout, uri=uri, factory=Connection)
    monkeypatch.setattr(module.sqlite3,"connect",connect)
    store=Store(tmp_path/"fresh.sqlite")
    assert len(attempts)==2 and attempts[0] is attempts[1]
    with store.connect() as db:
        assert db.execute("PRAGMA journal_mode").fetchone()[0]=="wal"
        assert db.execute("PRAGMA busy_timeout").fetchone()[0]==15000
        assert [r[0] for r in db.execute("SELECT version FROM auth_schema ORDER BY version")]==[1]

def test_permanent_wal_error_is_not_retried_or_hidden(tmp_path: Path,monkeypatch: pytest.MonkeyPatch) -> None:
    original=sqlite3.connect;attempts: list[object] = []
    class Connection(sqlite3.Connection):
        def execute(self, sql: str, parameters: Parameters | Mapping[str, object] = (), /) -> sqlite3.Cursor:
            if sql=="PRAGMA journal_mode=WAL":
                attempts.append(sql)
                error=sqlite3.OperationalError("disk I/O error");error.sqlite_errorcode=sqlite3.SQLITE_IOERR
                raise error
            return super().execute(sql,parameters)
    def connect(database: str | Path, *, timeout: float = 5, uri: bool = False) -> Connection:
        return original(database, timeout=timeout, uri=uri, factory=Connection)
    monkeypatch.setattr(module.sqlite3,"connect",connect)
    with pytest.raises(sqlite3.OperationalError,match="disk I/O error"):Store(tmp_path/"fresh.sqlite")
    assert len(attempts)==1

def test_wal_lock_retry_stops_at_startup_deadline(tmp_path: Path,monkeypatch: pytest.MonkeyPatch) -> None:
    original=sqlite3.connect;attempts: list[object] = [];clock=iter([0,0,15])
    class Connection(sqlite3.Connection):
        def execute(self, sql: str, parameters: Parameters | Mapping[str, object] = (), /) -> sqlite3.Cursor:
            if sql=="PRAGMA journal_mode=WAL":
                attempts.append(sql)
                error=sqlite3.OperationalError("database is locked");error.sqlite_errorcode=sqlite3.SQLITE_BUSY
                raise error
            return super().execute(sql,parameters)
    def connect(database: str | Path, *, timeout: float = 5, uri: bool = False) -> Connection:
        return original(database, timeout=timeout, uri=uri, factory=Connection)
    monkeypatch.setattr(module.sqlite3,"connect",connect)
    def unexpected_sleep(seconds: float) -> NoReturn:
        pytest.fail("Deadline exhausted")
    monkeypatch.setattr(module,"time",SimpleNamespace(monotonic=lambda:next(clock),sleep=unexpected_sleep),raising=False)
    with pytest.raises(sqlite3.OperationalError,match="database is locked"):Store(tmp_path/"fresh.sqlite")
    assert len(attempts)==1

def test_thirty_fresh_eight_worker_startups_have_no_lock_failures(tmp_path: Path) -> None:
    for trial in range(30):
        path=tmp_path/f"fresh-{trial}.sqlite";barrier=Barrier(8)
        def start(_: int) -> Store:
            barrier.wait(timeout=10)
            return Store(path)
        with ThreadPoolExecutor(8) as pool:stores=list(pool.map(start,range(8)))
        with stores[0].connect() as db:
            assert db.execute("PRAGMA journal_mode").fetchone()[0]=="wal"
            assert [r[0] for r in db.execute("SELECT version FROM auth_schema ORDER BY version")]==[1]
