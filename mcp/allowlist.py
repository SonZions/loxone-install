"""Build exact device/parameter entries from live structure; never sends actuator commands."""
import argparse
import asyncio
import json
from collections import Counter
from controls import controls, mappings, logic_connected
from core import Miniserver

def build(structure):
    allowed={}; counts=Counter(); excluded=[]
    for uuid, device in controls(structure).items():
        specs=mappings(device)
        if not specs:
            continue
        parent=device.get('_parent_control', {})
        if any(d.get('isSecured') or d.get('restrictions',0) for d in (device,parent)):
            excluded.append({'uuid':uuid,'reason':'restricted_device'});continue
        names=[k for k,v in specs.items() if not logic_connected(device,v)]
        if names:
            allowed[uuid]=names;counts[device['type']]+=1
        else:excluded.append({'uuid':uuid,'reason':'logic_connected'})
    return {'writes_enabled':True,'allowlist':allowed,'pilot_grant':None},dict(counts),excluded

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--config',default='/run/secrets/config.json');args=parser.parse_args()
    config=json.load(open(args.config))
    policy,counts,excluded=build(asyncio.run(Miniserver(config['loxone']).structure()))
    print(json.dumps({'policy':policy,'counts':counts,'excluded':excluded},indent=2))
