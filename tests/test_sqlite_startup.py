from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
import sqlite3
from types import SimpleNamespace
import pytest
import gateway.store as module
from gateway.store import Store

@pytest.mark.parametrize("code",[sqlite3.SQLITE_BUSY,sqlite3.SQLITE_LOCKED])
def test_initial_wal_lock_is_retried_on_same_connection(tmp_path,monkeypatch,code):
    original=sqlite3.connect;attempts=[]
    class Connection(sqlite3.Connection):
        def execute(self,sql,*args):
            if sql=="PRAGMA journal_mode=WAL":
                attempts.append(self)
                if len(attempts)==1:
                    error=sqlite3.OperationalError("database is locked");error.sqlite_errorcode=code
                    raise error
            return super().execute(sql,*args)
    monkeypatch.setattr(module.sqlite3,"connect",lambda *args,**kwargs:original(*args,factory=Connection,**kwargs))
    store=Store(tmp_path/"fresh.sqlite")
    assert len(attempts)==2 and attempts[0] is attempts[1]
    with store.connect() as db:
        assert db.execute("PRAGMA journal_mode").fetchone()[0]=="wal"
        assert db.execute("PRAGMA busy_timeout").fetchone()[0]==15000
        assert [r[0] for r in db.execute("SELECT version FROM schema_versions ORDER BY version")]==[1,2]

def test_permanent_wal_error_is_not_retried_or_hidden(tmp_path,monkeypatch):
    original=sqlite3.connect;attempts=[]
    class Connection(sqlite3.Connection):
        def execute(self,sql,*args):
            if sql=="PRAGMA journal_mode=WAL":
                attempts.append(sql)
                error=sqlite3.OperationalError("disk I/O error");error.sqlite_errorcode=sqlite3.SQLITE_IOERR
                raise error
            return super().execute(sql,*args)
    monkeypatch.setattr(module.sqlite3,"connect",lambda *args,**kwargs:original(*args,factory=Connection,**kwargs))
    with pytest.raises(sqlite3.OperationalError,match="disk I/O error"):Store(tmp_path/"fresh.sqlite")
    assert len(attempts)==1

def test_wal_lock_retry_stops_at_startup_deadline(tmp_path,monkeypatch):
    original=sqlite3.connect;attempts=[];clock=iter([0,0,15])
    class Connection(sqlite3.Connection):
        def execute(self,sql,*args):
            if sql=="PRAGMA journal_mode=WAL":
                attempts.append(sql)
                error=sqlite3.OperationalError("database is locked");error.sqlite_errorcode=sqlite3.SQLITE_BUSY
                raise error
            return super().execute(sql,*args)
    monkeypatch.setattr(module.sqlite3,"connect",lambda *args,**kwargs:original(*args,factory=Connection,**kwargs))
    monkeypatch.setattr(module,"time",SimpleNamespace(monotonic=lambda:next(clock),sleep=lambda _:pytest.fail("Deadline exhausted")),raising=False)
    with pytest.raises(sqlite3.OperationalError,match="database is locked"):Store(tmp_path/"fresh.sqlite")
    assert len(attempts)==1

def test_thirty_fresh_eight_worker_startups_have_no_lock_failures(tmp_path):
    for trial in range(30):
        path=tmp_path/f"fresh-{trial}.sqlite";barrier=Barrier(8)
        def start(_):
            barrier.wait(timeout=10)
            return Store(path)
        with ThreadPoolExecutor(8) as pool:stores=list(pool.map(start,range(8)))
        with stores[0].connect() as db:
            assert db.execute("PRAGMA journal_mode").fetchone()[0]=="wal"
            assert [r[0] for r in db.execute("SELECT version FROM schema_versions ORDER BY version")]==[1,2]
