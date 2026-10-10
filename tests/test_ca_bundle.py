"""Real ephemeral certificates; no operating-system trust stores changed."""
import hashlib, os, subprocess, sys, tempfile, unittest
from pathlib import Path
TOOL = Path(__file__).resolve().parents[1] / 'tools/make-ca-bundle.py'
class BundleTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(); cls.root = Path(cls.tmp.name)
        def certificate(name, ca=True):
            path=cls.root/(name+'.crt')
            subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-days','1','-subj','/CN='+name,'-keyout',str(cls.root/(name+'.key')),'-out',str(path),'-addext','basicConstraints=critical,CA:'+str(ca).upper(),'-addext','keyUsage=critical,'+('keyCertSign,cRLSign' if ca else 'digitalSignature')],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            return path
        cls.public1=certificate('public-one'); cls.public2=certificate('public-two'); cls.local=certificate('local'); cls.leaf=certificate('leaf',False)
        cls.public=cls.root/'public.crt'; cls.public.write_bytes(cls.public1.read_bytes()+cls.public2.read_bytes())
        cls.pin=hashlib.sha256(subprocess.check_output(['openssl','x509','-in',str(cls.local),'-outform','DER'])).hexdigest()
    @classmethod
    def tearDownClass(cls): cls.tmp.cleanup()
    def run_tool(self, local=None, pin=None, public=None, output=None):
        return subprocess.run([sys.executable,str(TOOL),'--public-roots',str(public or self.public),'--local-ca',str(local or self.local),'--expected-ca-sha256',pin or self.pin,'--output',str(output or self.root/'bundle.crt')],capture_output=True,text=True)
    def test_retains_all_roots_and_idempotent_dedup(self):
        out=self.root/'retained.crt'; result=self.run_tool(output=out); self.assertEqual(result.returncode,0,result.stderr)
        first=out.read_bytes(); stat=out.stat(); self.assertEqual(first.count(b'BEGIN CERTIFICATE'),3); self.assertEqual(stat.st_mode&0o777,0o644)
        self.assertEqual(self.run_tool(output=out).returncode,0); self.assertEqual(out.read_bytes(),first); self.assertEqual(out.stat().st_mtime_ns,stat.st_mtime_ns)
        public=self.root/'duplicates.crt'; public.write_bytes(self.public.read_bytes()+self.local.read_bytes()+self.public1.read_bytes())
        self.assertEqual(self.run_tool(public=public,output=out).returncode,0); self.assertEqual(out.read_bytes(),first)
    def test_wrong_pin_nonca_private_key_and_multiple_inputs_fail(self):
        for local,pin in [(self.local,'0'*64),(self.leaf,hashlib.sha256(subprocess.check_output(['openssl','x509','-in',str(self.leaf),'-outform','DER'])).hexdigest()),(self.root/'local.key',None)]:
            out=self.root/'reject.crt'; result=self.run_tool(local=local,pin=pin,output=out); self.assertNotEqual(result.returncode,0); self.assertFalse(out.exists())
        combined=self.root/'combined.crt'; combined.write_bytes(self.local.read_bytes()+self.public1.read_bytes())
        self.assertNotEqual(self.run_tool(local=combined).returncode,0)
        keyed=self.root/'keyed.crt'; keyed.write_bytes(self.local.read_bytes()+(self.root/'local.key').read_bytes())
        self.assertNotEqual(self.run_tool(local=keyed).returncode,0)
    def test_key_usage_and_self_signature_required(self):
        no_usage=self.root/'no-usage.crt'
        subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-days','1','-subj','/CN=no-usage','-keyout',str(self.root/'no-usage.key'),'-out',str(no_usage),'-addext','basicConstraints=critical,CA:TRUE'],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        der=subprocess.check_output(['openssl','x509','-in',str(no_usage),'-outform','DER'])
        result=self.run_tool(local=no_usage,pin=hashlib.sha256(der).hexdigest())
        self.assertNotEqual(result.returncode,0); self.assertIn('keyCertSign',result.stderr)
        broken=bytearray(subprocess.check_output(['openssl','x509','-in',str(self.local),'-outform','DER'])); broken[-1]^=1
        pem=subprocess.check_output(['openssl','x509','-inform','DER','-outform','PEM'],input=broken)
        corrupt=self.root/'corrupt-signature.crt'; corrupt.write_bytes(pem)
        result=self.run_tool(local=corrupt,pin=hashlib.sha256(broken).hexdigest())
        self.assertNotEqual(result.returncode,0); self.assertIn('signature',result.stderr)
    def test_symlinks_and_relative_output_fail(self):
        link=self.root/'link.crt'; link.symlink_to(self.local)
        self.assertNotEqual(self.run_tool(local=link).returncode,0)
        out=self.root/'out-link.crt'; out.symlink_to(self.public1)
        before=self.public1.read_bytes(); self.assertNotEqual(self.run_tool(output=out).returncode,0); self.assertEqual(self.public1.read_bytes(),before)
        self.assertNotEqual(self.run_tool(output=Path('relative.crt')).returncode,0)
        parent=self.root/'dir-link'; parent.symlink_to(self.root,target_is_directory=True)
        self.assertNotEqual(self.run_tool(output=parent/'new.crt').returncode,0)
if __name__=='__main__': unittest.main()
