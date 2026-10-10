#!/usr/bin/env python3
"""Build a pinned public CA bundle without changing host trust; Python stdlib + OpenSSL."""
import argparse, datetime, hashlib, os, re, subprocess, tempfile
from pathlib import Path

PEM = re.compile(rb'-----BEGIN CERTIFICATE-----\s+[A-Za-z0-9+/=\r\n]+-----END CERTIFICATE-----')
def fail(message): raise ValueError(message)
def safe_path(value, output=False):
    path=Path(value)
    if '..' in path.parts or output and not path.is_absolute(): fail('Output must be absolute; paths must not contain ..')
    path=path.absolute()
    for part in [*reversed(path.parents),path]:
        if part.is_symlink(): fail('Symlink paths are forbidden')
    if output:
        if not path.parent.is_dir() or path.exists() and not path.is_file(): fail('Output requires existing directory and a regular file destination')
    elif not path.is_file(): fail('Input must be a regular public certificate file')
    return path

def openssl(*args, data=None):
    result=subprocess.run(['openssl',*args],input=data,capture_output=True,env={**os.environ,'LC_ALL':'C'})
    if result.returncode: fail('OpenSSL rejected certificate or signature')
    return result.stdout

def certificates(path, single=False):
    content=path.read_bytes()
    if b'PRIVATE KEY' in content: fail('Private key material is forbidden')
    blocks=PEM.findall(content)
    remainder=PEM.sub(b'',content)
    if not single: remainder=b'\n'.join(line for line in remainder.splitlines() if not line.lstrip().startswith(b'#'))
    if remainder.strip() or not blocks or single and len(blocks)!=1: fail('Expected '+('one public CA certificate' if single else 'public PEM certificates only'))
    return [openssl('x509','-inform','PEM','-outform','DER',data=block) for block in blocks]

def validated_local(path, pin):
    der=certificates(path,single=True)[0]
    if not re.fullmatch('[0-9a-fA-F]{64}',pin): fail('Expected CA SHA-256 must be 64 hex digits of certificate DER')
    if hashlib.sha256(der).hexdigest()!=pin.lower(): fail('Local CA certificate SHA-256 mismatch')
    text=openssl('x509','-inform','DER','-noout','-text',data=der).decode('ascii')
    if not re.search(r'X509v3 Basic Constraints:[^\n]*\n\s*CA:TRUE(?:[,\s]|$)',text): fail('Local certificate must declare CA:TRUE')
    usage=re.search(r'X509v3 Key Usage:[^\n]*\n\s*([^\n]+)',text)
    if not usage or 'Certificate Sign' not in usage[1]: fail('Local CA must explicitly permit keyCertSign')
    dates=openssl('x509','-inform','DER','-noout','-dates',data=der).decode('ascii').splitlines()
    validity={k:datetime.datetime.strptime(v,'%b %d %H:%M:%S %Y GMT').replace(tzinfo=datetime.timezone.utc) for k,v in (line.split('=',1) for line in dates)}
    now=datetime.datetime.now(datetime.timezone.utc)
    if not validity['notBefore']<=now<validity['notAfter']: fail('Local CA is not currently valid')
    identity=openssl('x509','-inform','DER','-noout','-subject','-issuer','-nameopt','RFC2253',data=der).decode('ascii').splitlines()
    if identity[0].split('=',1)[1]!=identity[1].split('=',1)[1]: fail('Local CA must be self-issued')
    # Pin alone is not a signature check. Verify the self-signature with only this trust anchor.
    openssl('verify','-no-CAfile','-no-CApath','-trusted',str(path),'-check_ss_sig',str(path))
    return der

def build(public_roots, local_ca, pin, output):
    roots=safe_path(public_roots); local=safe_path(local_ca); target=safe_path(output,output=True)
    if target in (roots,local): fail('Output must not overwrite an input certificate file')
    all_certs=certificates(roots)+[validated_local(local,pin)]
    seen=set(); bundle=[]
    for der in all_certs:
        if der in seen: continue
        seen.add(der); bundle.append(openssl('x509','-inform','DER','-outform','PEM',data=der))
    data=b''.join(bundle)
    if target.exists() and target.read_bytes()==data:
        target.chmod(0o644); return len(bundle),False
    fd,name=tempfile.mkstemp(prefix='.'+target.name+'-',dir=target.parent)
    try:
        with os.fdopen(fd,'wb') as stream:
            stream.write(data); stream.flush(); os.fchmod(stream.fileno(),0o644); os.fsync(stream.fileno())
        # Recheck destination/parents before the atomic replacement.
        safe_path(target,output=True); os.replace(name,target)
    finally:
        if os.path.exists(name): os.unlink(name)
    return len(bundle),True

def main():
    p=argparse.ArgumentParser(description=__doc__)
    for name in ('public-roots','local-ca','expected-ca-sha256','output'): p.add_argument('--'+name,required=True)
    a=p.parse_args()
    try:
        count,changed=build(a.public_roots,a.local_ca,a.expected_ca_sha256,a.output)
    except (ValueError,OSError) as error: p.exit(1,'CA bundle refused: '+str(error)+'\n')
    print('Public CA bundle '+('written' if changed else 'unchanged')+'; '+str(count)+' distinct certificates; mode 0644')
if __name__=='__main__': main()
