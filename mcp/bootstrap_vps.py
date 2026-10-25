"""Run once on VPS: reuse existing local Loxone credentials, never export them."""
import argparse,hashlib,json,os,secrets,subprocess
from pathlib import Path
from urllib.parse import urlsplit,urlunsplit
parser=argparse.ArgumentParser();parser.add_argument('--expected-serial',required=True);args=parser.parse_args()
root=Path('/etc/loxone-mcp');root.mkdir(mode=0o700,exist_ok=True)
data=Path('/var/lib/loxone-mcp');data.mkdir(mode=0o700,exist_ok=True);os.chown(data,10001,10001)
config=root/'config.json'
if config.exists():raise SystemExit('Already configured; refusing to overwrite secrets')
c=json.loads(subprocess.check_output(['docker','inspect','awstates-vps']))[0]
e=dict(v.split('=',1) for v in c['Config']['Env'])
u=urlsplit(e['LOXONE_URL']);base=urlunsplit((u.scheme,u.netloc,'','',''))
password=secrets.token_urlsafe(32);secret=secrets.token_urlsafe(32);salt=secrets.token_bytes(16)
d={'loxone':{'url':base,'username':e['LOXONE_USERNAME'],'password':e['LOXONE_PASSWORD'],'serial':args.expected_serial},
   'oauth':{'public_url':'https://not-configured.invalid','client_id':'loxone-chatgpt',
            'client_secret_hash':hashlib.sha256(secret.encode()).hexdigest(),'redirect_uris':[],
            'password_salt':salt.hex(),'password_hash':hashlib.scrypt(password.encode(),salt=salt,n=16384,r=8,p=1).hex()},
   'audit_db':'/data/audit.sqlite','auth_db':'/data/auth.sqlite','policy_path':'/run/secrets/policy.json'}
for path,obj in [(config,d),(root/'policy.json',{'writes_enabled':False,'allowlist':{},'pilot_grant':None})]:
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o400)
    with os.fdopen(fd,'w') as f:json.dump(obj,f)
    os.chown(path,10001,10001)
fd=os.open(root/'owner-credentials.json',os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
with os.fdopen(fd,'w') as f:json.dump({'client_id':'loxone-chatgpt','client_secret':secret,'login_password':password},f)
print('Configured: secrets stay on VPS, writes disabled, public OAuth pending hostname and callback')
