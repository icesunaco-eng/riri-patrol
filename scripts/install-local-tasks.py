#!/usr/bin/env python3
"""Deploy only the reviewed local-run code by fast-forward; keep live state and service."""
import argparse
from contextlib import ExitStack
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys

LIVE=Path('/Users/sunako.jp/riri-patrol')
BASE='3eb25d18d0c0a4ce4c59e5e6e20a957caff7f810'
PREVIOUS='6955ba35dff08b64073fc6595736708a2398c3af'
DEPLOYED='04190cddc95eb29b04667143ff1da6c758cd6f3a'
VERSIONED='7ef39089d2b16c7ee40f8171f8e37d18e7023a4a'
BRANCH='release/riri-3000-20261115'
FILES={'riri/local_tasks.py','riri/patrol.py','docs/local-tasks.md','scripts/install-local-tasks.py','tests/test_local_tasks.py','riri/local_revisions.py','tests/test_local_revisions.py'}

def git(*args):
    return subprocess.check_output(['git','-C',str(LIVE),*args]).decode().strip()

def check(cond,reason):
    if not cond:
        raise SystemExit(reason)

def main():
    args=argparse.ArgumentParser()
    args.add_argument('commit')
    args.add_argument('--apply',action='store_true')
    opts=args.parse_args()
    commit=opts.commit
    check(re.fullmatch('[0-9a-f]{40}',commit),'full commit required')
    check(git('symbolic-ref','--short','HEAD')==BRANCH,'unexpected live branch')
    check(git('status','--porcelain')=='','live checkout has other changes')
    previous=git('rev-parse','HEAD')
    check(previous in (BASE,PREVIOUS,DEPLOYED,VERSIONED,commit),'live checkout changed')
    check(git('merge-base',previous,commit)==previous,'cannot fast-forward current release')
    check(git('merge-base',BASE,commit)==BASE,'requested commit is not a fast-forward')
    check(set(git('diff','--name-only',BASE,commit).splitlines())==FILES,'unexpected deployment files')
    check(git('config','--get','remote.origin.url') in ('https://github.com/icesunaco-eng/riri-patrol.git','git@github.com:icesunaco-eng/riri-patrol.git'),'different remote')
    sources={p:subprocess.check_output(['git','-C',str(LIVE),'show',commit+':'+p]) for p in FILES}
    manifest={p:hashlib.sha256(v).hexdigest() for p,v in sorted(sources.items())}
    if opts.apply:
        with ExitStack() as locks:
            paths=[LIVE/'state/patrol.lock']
            actor_lease=LIVE/'state/local-tasks/actor.lock'
            if actor_lease.exists():
                paths.append(actor_lease)
            for path in paths:
                fd=os.open(path,os.O_RDWR|os.O_NOFOLLOW)
                locks.callback(os.close,fd)
                fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            check(git('status','--porcelain')=='' and git('rev-parse','HEAD')==previous,'live changed after check')
            subprocess.run(['git','-C',str(LIVE),'merge','--ff-only',commit],check=True,stdout=sys.stderr)
            for path,data in sources.items():
                check((LIVE/path).read_bytes()==data,'installed content differs: '+path)
            probe='from pathlib import Path; import json,riri.local_tasks,riri.local_revisions;print(json.dumps([m.__file__ for m in (riri.local_tasks,riri.local_revisions)]))'
            found=json.loads(subprocess.check_output([sys.executable,'-B','-c',probe],cwd=LIVE,env={'PATH':'/usr/bin:/bin','PYTHONDONTWRITEBYTECODE':'1'}))
            check(found==[str(LIVE/p) for p in ('riri/local_tasks.py','riri/local_revisions.py')],'fresh-process verification failed')
    print(json.dumps({'applied':opts.apply,'source_commit':commit,'previous_commit':previous,'manifest':manifest},indent=2))

if __name__=='__main__':
    main()
