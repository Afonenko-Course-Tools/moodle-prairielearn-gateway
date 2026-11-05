"""Вымышленные настоящие схемы v3/v4 для проверки миграции."""
from pathlib import Path
from gateway.store import digest

def legacy_schema(version):
    schema=Path('schema/runtime.sql').read_text().replace('VALUES(5)',f'VALUES({version})').replace('VALUES(4)',f'VALUES({version})')
    schema=schema.replace(', email TEXT','')
    if version==3:schema=schema.replace('lineitem TEXT,','lineitem TEXT NOT NULL,')
    return schema


def legacy_person(store,subject='student-a'):
    key=digest(['issuer','client','deploy',subject])
    identity={'userId':key,'issuer':'issuer','client':'client','deployment':'deploy','subject':subject,'uid':'gw-'+key+'@example.invalid',
              'name':subject,'role':'student','context':'context','resource':'defense','activity':'defense','lineitem':'https://moodle.example/lineitem',
              'token':'legacy-session-'+subject,'csrf':'legacy-csrf'}
    with store.transaction() as db:
        db.execute('INSERT INTO users VALUES(?,?,?,?,?,?,?)',tuple(identity[k] for k in ('userId','issuer','client','deployment','subject','uid','name')))
        db.execute('INSERT INTO sessions VALUES(?,?,?,?,?,?,?,?,?)',(digest(identity['token']),key,'student','context','defense','defense',identity['lineitem'],4600,identity['csrf']))
        db.execute('INSERT INTO activity_bindings VALUES(?,?,?,?,?)',(key,'defense','context','defense',identity['lineitem']))
    return identity
