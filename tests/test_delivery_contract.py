import hashlib
from gateway.store import native_digest

def test_native_canonical_hash_matches_exporter_unicode_scalar_and_number_text():
    # Exact canonical text emitted by the owner JS scalar comparator/JSON.stringify.
    value={"é":"Защита", "10":1.0, "2":1e-7, "small":1e-6, "big":1e20, "huge":1e21, "negativeZero":-0.0, "😀":{"a":0.5}}
    text='{"10":1,"2":1e-7,"big":100000000000000000000,"huge":1e+21,"negativeZero":0,"small":0.000001,"é":"Защита","😀":{"a":0.5}}'
    assert native_digest(value)==hashlib.sha256(text.encode()).hexdigest()
