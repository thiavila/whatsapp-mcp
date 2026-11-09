"""Read-only gateway health monitor, independent of the gateway and deploy poll.

Never starts/stops services or opens application databases. Delivery uses the
existing principal's CLI without an LLM. Incident state is private controller data.
"""
import datetime as dt
import json
import time

import executor as common


def maintenance_active(now):
    base=common.BASE.parent/'hermes-deploy'
    try:
        if (base/'intervention.json').exists():return False
        tx=json.loads((base/'pending.json').read_text())
        start=tx['started_at'];deadline=tx['maintenance_deadline']
        return (tx.get('phase') in ['stopping','installing','starting']
                and 0<deadline-start<=2400 and start<=now<deadline)
    except (OSError,ValueError,KeyError,TypeError):return False


def problem(now):
    try:
        properties=common.run(['systemctl','--user','show','hermes-gateway.service',
                               '-p','ActiveState','-p','MainPID'])
        service=dict(line.split('=',1) for line in properties.splitlines())
        if service.get('ActiveState')!='active':return 'gateway-inactive'
        state=json.loads((common.HERMES/'gateway_state.json').read_text())
        if int(service['MainPID'])!=state.get('pid'):return 'gateway-state-stale'
        updated=dt.datetime.fromisoformat(state['updated_at']).timestamp()
        if not -30<=now-updated<=180:return 'gateway-state-stale'
        if state.get('gateway_state')!='running':return 'gateway-not-running'
        platforms=state.get('platforms',{})
        telegram=[v for k,v in platforms.items() if k=='telegram' or k.endswith(':telegram')]
        if not telegram or any(p.get('state')!='connected' for p in telegram):
            return 'telegram-disconnected'
    except (OSError,ValueError,KeyError,TypeError,common.subprocess.SubprocessError):
        return 'gateway-health-unavailable'
    return None


def check(now=None):
    now=time.time() if now is None else now
    path=common.BASE/'hermes-health-incident.json'
    reason=problem(now)
    if reason is None:
        path.unlink(missing_ok=True)
        return
    if maintenance_active(now):return
    try:
        previous=json.loads(path.read_text())
        if (not isinstance(previous,dict) or not isinstance(previous.get('since'),(int,float))
                or not isinstance(previous.get('id'),str) or not 0<=previous['since']<=now):
            previous={}
    except (OSError,ValueError):previous={}
    incident=previous or {'since':now,'id':str(time.time_ns())}
    incident['reason']=reason
    common.atomic(path,incident)
    # Covers normal startup/maintenance; an indefinitely pending deployment
    # cannot suppress this independent alert. Same outage notifies only once.
    if now-incident['since']>=300:
        try:
            common.notify('hermes-health-'+incident['id'],
                'Hermes está sem saúde confirmada há pelo menos 5 minutos ('+reason+'). '
                'Confira gateway/Telegram e manutenção na VM. Este monitor não alterou serviços nem dados.')
        except (OSError,ValueError,common.subprocess.SubprocessError):
            incident['delivery_failed']=True
            common.atomic(path,incident)
            print('Hermes health alert delivery failed; incident retained for retry',flush=True)
            raise


if __name__=='__main__':
    common.BASE.mkdir(mode=0o700,parents=True,exist_ok=True)
    check()
