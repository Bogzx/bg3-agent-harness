"""Combat review of a run's transcripts: per fight, rounds, combat_turn vs single-step calls, failures inside
combat_turn by reason, unused action/bonus, end_turn timeouts, agent think vs tool time.
Fights are detected heuristically (first result mentioning a round/TURN until "combat is over").

Run: python3 tools/dev/combat_review.py runtime/sessions/claude-run-2
"""
import json,re,sys,glob,collections,datetime
SINGLE={'use_ability','end_turn','move_to','attack','walk_toward','do_actions','jump_to'}
def ts(s):
    return datetime.datetime.fromisoformat(s.replace('Z','+00:00')).timestamp()
def text(b):
    c=b.get('content')
    if isinstance(c,str): t=c
    else: t=' '.join(x.get('text','') for x in (c or []) if isinstance(x,dict))
    try: t=json.loads(t).get('result',t)
    except Exception: pass
    return t
for f in sorted(glob.glob(sys.argv[1]+'/S0*.jsonl')):
    if re.search(r'\.(metrics|turns|progress)\.jsonl$',f): continue
    uses={}; ev=[]
    for l in open(f):
        d=json.loads(l)
        if d.get('type')=='assistant':
            for b in d['message']['content']:
                if b.get('type')=='tool_use':
                    uses[b['id']]=(b['name'].split('__')[-1],b['input'],ts(d['timestamp']))
        elif d.get('type')=='user' and isinstance(d['message']['content'],list):
            for b in d['message']['content']:
                if b.get('type')=='tool_result' and b['tool_use_id'] in uses:
                    n,i,t0=uses[b['tool_use_id']]; ev.append((n,i,t0,ts(d['timestamp']),text(b)))
    # fights: from a result that says combat mode/TURN until "combat ended"/MODE ... -> EXPLORE
    fights=[];cur=None;lastres=None
    for n,i,t0,t1,t in ev:
        inc = bool(re.search(r'COMBAT turn-based|TURN e\d+ |round \d+',t))
        if inc and cur is None:
            cur=dict(start=t0,ct=0,single=collections.Counter(),fail=collections.Counter(),rounds=set(),turns=[],downs=0,bonus_used=0,bonus_left=0,think=[],exec=[],tools=collections.Counter(),endto=0,last=lastres)
            fights.append(cur)
        if cur is not None:
            cur['tools'][n]+=1
            if lastres: cur['think'].append(t0-lastres)
            cur['exec'].append(t1-t0)
            for r in re.findall(r'round (\d+)',t): cur['rounds'].add(int(r))
            if n=='combat_turn':
                cur['ct']+=1
                for m in re.finditer(r'(?m)^(move|action|bonus|extra)[^\n]*',t):
                    line=m.group(0)
                    for fm in re.finditer(r'FAILED — ([^→\n]*)',line):
                        reason=re.sub(r'[\d.]+','N',fm.group(1))[:60]; cur['fail'][m.group(1)+': '+reason]+=1
                    if 'skipped' in line: cur['fail'][m.group(1)+': skipped '+re.sub(r'e\d+','eX',line.split('skipped')[1])[:40]]+=1
                if re.search(r'(?m)^bonus.*done',t): cur['bonus_used']+=1
                nw=re.search(r'NOW e\d+ hp=\S+ max_hp=\S+ action=(\d) bonus=(\d) movement=([\d.]+)',t)
                if nw: cur['turns'].append((int(nw.group(1)),int(nw.group(2)),float(nw.group(3)))); cur['bonus_left']+=nw.group(2)=='1'
                if 'end_turn: TIMEOUT' in t: cur['endto']+=1
            elif n in SINGLE: cur['single'][n]+=1
            cur['downs']+=len(re.findall(r'\+Downed|DOWNED|downed',t))
            if re.search(r'combat (ended|over)|-> EXPLOR|COMBAT[^\n]*-> (FREE|EXPLORE)',t):
                cur['end']=t1; cur=None
        lastres=t1
    for k,fi in enumerate(fights):
        dur=(fi.get('end') or lastres)-fi['start']
        th=sorted(fi['think']); ex=sorted(fi['exec'])
        med=lambda a:a[len(a)//2] if a else 0
        print(f"{f.split('/')[-1]} fight{k+1}: dur={dur:.0f}s rounds={max(fi['rounds'] or [0])} combat_turn={fi['ct']} single={dict(fi['single'])} turns={len(fi['turns'])} action_left={sum(1 for a,b,m in fi['turns'] if a)} bonus_left={fi['bonus_left']} bonus_used={fi['bonus_used']} endturn_timeout={fi['endto']} downs~{fi['downs']} think_med={med(th):.1f}s think_sum={sum(th):.0f}s exec_med={med(ex):.1f}s exec_sum={sum(ex):.0f}s")
        for r,c in fi['fail'].most_common(): print('    ',c,r)
        print('    tools',dict(fi['tools'].most_common(8)))
