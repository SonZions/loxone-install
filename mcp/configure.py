"""Interactive VPS-only configuration. Secrets are read without terminal echo."""
import argparse,getpass,hashlib,json,os,secrets
from pathlib import Path
from urllib.parse import urlsplit
p=argparse.ArgumentParser()
p.add_argument('--public-url');p.add_argument('--redirect-uri');p.add_argument('--service-user',action='store_true');p.add_argument('--rotate-oauth',action='store_true')
a=p.parse_args();path=Path('/etc/loxone-mcp/config.json');c=json.loads(path.read_text())
if a.public_url:
    u=urlsplit(a.public_url)
    if u.scheme!='https' or not u.hostname or u.username or u.password or u.path not in ('','/') or u.query or u.fragment:
        p.error('public-url must be an HTTPS origin')
    c['oauth']['public_url']=a.public_url.rstrip('/')
if a.redirect_uri:
    u=urlsplit(a.redirect_uri)
    if u.scheme!='https' or u.hostname!='chatgpt.com' or u.username or u.password or u.fragment:
        p.error('Use the exact HTTPS callback shown by ChatGPT on chatgpt.com')
    c['oauth']['redirect_uris']=[a.redirect_uri]
if a.service_user:
    c['loxone']['username']=input('Dedicated Loxone service username: ').strip()
    c['loxone']['password']=getpass.getpass('Loxone service password (not echoed): ')
    if not c['loxone']['username'] or not c['loxone']['password']:p.error('Credentials cannot be empty')
if a.rotate_oauth:
    password=getpass.getpass('New MCP login password (at least 20 characters): ')
    if len(password)<20 or password!=getpass.getpass('Repeat password: '):p.error('Password mismatch or too short')
    secret=getpass.getpass('New OAuth client secret (at least 32 random characters): ')
    if len(secret)<32:p.error('Secret too short')
    salt=secrets.token_bytes(16);c['oauth']['password_salt']=salt.hex()
    c['oauth']['password_hash']=hashlib.scrypt(password.encode(),salt=salt,n=16384,r=8,p=1).hex()
    c['oauth']['client_secret_hash']=hashlib.sha256(secret.encode()).hexdigest()
    # Revoke access and refresh tokens when rotating.
    import sqlite3
    db=sqlite3.connect('/var/lib/loxone-mcp/auth.sqlite');db.execute('DELETE FROM grants');db.commit();db.close()
    old=Path('/etc/loxone-mcp/owner-credentials.json')
    if old.exists():old.unlink()
# Keep the inode: existing Docker bind mounts see the updated file on restart.
os.chmod(path,0o600)
with path.open('w') as f:json.dump(c,f);f.flush();os.fsync(f.fileno())
os.chown(path,10001,10001);os.chmod(path,0o400)
print('Configuration saved. Restart only the MCP container to apply it.')
