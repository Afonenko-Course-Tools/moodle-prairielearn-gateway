from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
import asyncio
import pytest
from gateway.store import Store
from gateway.assignments import Assignments
from gateway.errors import GatewayError
from test_assignment import Bridge, person, delivery

def test_atomic_concurrent_capacity_and_duplicate_selection(tmp_path):
    store=Store(tmp_path/"runtime.sqlite"); a=person(store);b=person(store,"student-b")
    service=Assignments(store,Bridge(),delivery(),"pilot",{"mode":"immediate","capacity":{"exr-essay-a":1}},clock=lambda:1000)
    def choose(who):
        try:return asyncio.run(service.request(who,"exr-essay-a","concurrent"))["state"]
        except GatewayError:return "conflict"
    with ThreadPoolExecutor(2) as pool: outcomes=list(pool.map(choose,(a,b)))
    assert sorted(outcomes)==["assigned","conflict"]
    with Store(store.path).connect() as db:assert db.execute("SELECT count(*) FROM assignments").fetchone()[0]==1

def test_current_schema_and_expired_auth_after_restart(tmp_path):
    store=Store(tmp_path/"runtime.sqlite");who=person(store)
    restarted=Store(store.path)
    assert restarted.session(who["token"],1001)["subject"]=="student-a"
    with pytest.raises(GatewayError):restarted.session(who["token"],4600)
    with restarted.connect() as db:assert [r[0] for r in db.execute("SELECT version FROM schema_versions ORDER BY version")]==[3]

def test_concurrent_fresh_schema_creations_are_serialized(tmp_path):
    path=tmp_path/"fresh.sqlite";barrier=Barrier(8)
    def start(_):
        barrier.wait()
        return Store(path)
    with ThreadPoolExecutor(8) as pool: stores=list(pool.map(start,range(8)))
    with stores[0].connect() as db:
        assert [r[0] for r in db.execute("SELECT version FROM schema_versions ORDER BY version")]==[3]
