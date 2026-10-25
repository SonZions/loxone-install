"""Run interactively on VPS. No key in command arguments, logs, or chat."""
import getpass,hashlib,json,os,re,secrets,time
from pathlib import Path

def write_private(path,content,uid):
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
    with os.fdopen(fd,'w') as f:f.write(content);f.flush();os.fsync(f.fileno())
    os.chown(path,uid,uid);os.chmod(path,0o400)

if __name__=='__main__':
    if os.geteuid()!=0:raise SystemExit('Run on the VPS as root.')
    print('Configure only a tunnel whose OpenAI workspace/organization access you have reviewed.')
    tid=input('Exact tunnel ID from OpenAI: ').strip()
    if not re.fullmatch(r'tunnel_[A-Za-z0-9_-]{8,150}',tid):raise SystemExit('Invalid tunnel ID')
    key=getpass.getpass('Tunnel runtime API key (not admin key; input hidden): ').strip()
    if not key or any(ch.isspace() for ch in key):raise SystemExit('Invalid runtime key')
    root=Path('/etc/loxone-mcp/tunnel');root.mkdir(mode=0o700,exist_ok=True)
    os.chown(root,10002,10002)
    local=secrets.token_urlsafe(48)
    write_private(root/'runtime-key',key+'\n',10002)
    write_private(root/'backend-authorization','Bearer '+local+'\n',10002)
    cfg={'config_version':1,'control_plane':{'base_url':'https://api.openai.com','tunnel_id':tid,
        'api_key':'file:/run/tunnel/runtime-key','max_inflight_requests':4},
        'log':{'level':'warn','format':'json'},'health':{'listen_addr':'127.0.0.1:18090'},
        'admin_ui':{'open_browser':False},'mcp':{'server_urls':[{'channel':'main','url':'http://127.0.0.1:8080/mcp'}],
        'extra_headers':{'Authorization':'file:/run/tunnel/backend-authorization'},'max_concurrent_requests':2}}
    write_private(root/'config.json',json.dumps(cfg,indent=2)+'\n',10002)
    path=Path('/etc/loxone-mcp/config.json');c=json.loads(path.read_text())
    c['oauth']['tunnel']={'enabled':True,'token_hash':hashlib.sha256(local.encode()).hexdigest(),'expires_at':time.time()+30*86400}
    write_private(path,json.dumps(c),10001)
    print('Saved. Local tunnel credential is READ-ONLY and expires in 30 days. No secret printed.')
    print('Restart MCP, then run the tunnel doctor before starting the tunnel service.')
