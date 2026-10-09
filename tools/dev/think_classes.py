#!/usr/bin/env python3
"""Think time and output tokens per turn class from a play transcript (runtime/sessions/<pt>/Sxxx.jsonl).
Classes: routine (explore, dialogue filler, standard combat), decision (real dialogue choice, build/rest),
stuck/recover (clicks, screenshots, jumps), meta (memory, clock, saves, wiki), plus turns a bridge autopilot
could remove. A turn = one model request; think = time from the previous tool result to its last block;
tokens = thinking estimate + tool input/text size / 3.5 (stream-json usage is partial).
Usage: python3 tools/dev/think_classes.py runtime/sessions/claude-run-2/S00{2,3,5}.jsonl"""
import datetime
import json
import re
import statistics as stt
import sys
def ts(s): return datetime.datetime.fromisoformat(s.replace('Z','+00:00')).timestamp()
def steps(path):
    msgs={}; order=[]; results={}; last_user_t=None; prev_end=None
    rows=[]
    lines=[json.loads(l) for l in open(path)]
    tt=0
    for d in lines:
        t=d.get('timestamp')
        if d.get('subtype')=='thinking_tokens': tt=d['estimated_tokens']; continue
        if d.get('type')=='assistant':
            m=d['message']; mid=m['id']
            if mid not in msgs:
                msgs[mid]={'id':mid,'t0':ts(t),'t1':ts(t),'tools':[],'text':'','think':0,'out':0,'prev_user_t':last_user_t,'prev_results':[],'tt':tt}
                tt=0
                order.append(mid)
            s=msgs[mid]; s['t1']=ts(t); s['out']=max(s['out'],m['usage'].get('output_tokens',0))
            for b in m['content']:
                if b['type']=='tool_use': s['tools'].append((b['name'].replace('mcp__bg3__',''),b['input'],b['id']))
                elif b['type']=='text': s['text']+=b['text']
                elif b['type']=='thinking': s['think']+=len(b.get('thinking',''))
        elif d.get('type')=='user':
            last_user_t=ts(t) if t else last_user_t
            c=d['message']['content']
            if isinstance(c,list):
                for b in c:
                    if b.get('type')=='tool_result':
                        txt=b['content'] if isinstance(b['content'],str) else ''.join(x.get('text','') for x in b['content'] if isinstance(x,dict))
                        try: txt=json.loads(txt).get('result',txt)
                        except Exception: pass
                        results[b['tool_use_id']]=txt
    out=[]
    prev=None
    for mid in order:
        s=msgs[mid]
        s['prev_tools']=prev['tools'] if prev else []
        s['prev_res']=[results.get(x[2],'') for x in s['prev_tools']]
        s['res']=[results.get(x[2],'') for x in s['tools']]
        base=s['prev_user_t'] or s['t0']
        s['gap']=s['t0']-base if prev else 0
        s['dur']=s['t1']-base if prev else 0
        s['out_est']=s['tt']+round((len(s['text'])+sum(len(json.dumps(x[1]))+len(x[0])+10 for x in s['tools']))/3.5)
        out.append(s); prev=s
    return out
DLG={'get_dialogue','dialogue_advance','choose_dialogue','talk_to'}
EXP={'travel_to','walk_toward','move_to','wait_for','queue_actions','do_actions','look','get_nearby','get_frontier','get_status','get_map','route','interact','loot','loot_area','cancel_queue','loot_corpse','pick_up','regroup'}
CMB={'combat_turn','end_turn','get_combat_state','get_actions','use_ability'}
STK={'screenshot','click','click_on','jump_to','path_preview','press_key','ui_dump','ui_action','dismiss_popups','look_at','zoom'}
META={'Read','Edit','Write','ToolSearch','session_clock','save_game','load_game','wiki_page','wiki_search','get_inventory','list_saves'}
DEC={'level_up','get_level_up_options','short_rest','long_rest','camp'}
def nopts(txt):
    m=re.search(r'DIALOGUE [^\n]*\n((?:  \d+\. [^\n]*\n?)+)',txt)
    return len(re.findall(r'^  \d+\.',m.group(1),re.M)) if m else 0
def classify(s, prev_txt):
    names={x[0] for x in s['tools']}
    in_dlg = 'DIALOGUE ' in prev_txt or 'screen=dialogue' in prev_txt or 'dialogue_started' in prev_txt
    if names & DEC: return 'decision:build/rest'
    if 'choose_dialogue' in names:
        return 'decision:dialogue' if nopts(prev_txt)>=2 else 'routine:dialogue'
    if names & DLG or 'roll_check' in names or (names=={'press_key'} and in_dlg) : return 'routine:dialogue'
    if names & CMB: return 'routine:combat'
    if names & STK: return 'stuck/recover'
    if names & EXP: return 'routine:explore'
    if names <= META: return 'meta(memory/clock/save/wiki)'
    return 'other'
def avoidable(s, prev_txt, prev_names):
    names=[x[0] for x in s['tools']]; res='\n'.join(s['res'])
    if names==['get_dialogue']: return 'get_dialogue poll'
    if names and set(names)<= {'dialogue_advance','session_clock'} and re.search(r'0 filler line\(s\)\nSTOPPED: (line still playing|no dialogue|conversation ended|loading)',res): return 'empty dialogue_advance'
    if names and names[0]=='dialogue_advance' and '0 filler' in res and 'real choice' in res: return 'advance->choice (opening)'
    if names==['wait_for'] and prev_names and prev_names[0] in ('travel_to','walk_toward','move_to','queue_actions','talk_to','interact'): return 'wait right after issue'
    if names==['end_turn']: return 'separate end_turn'
    if names in (['session_clock'],): return 'clock only'
    if names==['loot'] and prev_names and prev_names[0] in ('interact','loot_corpse'): return 'loot after open'
    if names==['dismiss_popups']: return 'dismiss popups'
    return None
rows=[]
for S in sys.argv[1:]:
    st=steps(S)
    prev_txt=''; prev_names=[]
    for i,s in enumerate(st):
        if i==0: prev_txt='\n'.join(s['res']); prev_names=[x[0] for x in s['tools']]; continue
        if not s['tools']: continue  # final text
        c=classify(s,prev_txt); a=avoidable(s,prev_txt,prev_names)
        rows.append(dict(S=S,cls=c,av=a,dur=s['dur'],out=s['out_est'],think=s['think'],names=[x[0] for x in s['tools']]))
        prev_txt='\n'.join(s['res']); prev_names=[x[0] for x in s['tools']]
from collections import defaultdict
g=defaultdict(list)
for r in rows: g[r['cls']].append(r)
print(f"{'class':32s} {'n':>4s} {'med_s':>6s} {'mean_s':>6s} {'p90_s':>6s} {'sum_s':>6s} {'out_mean':>8s}")
for k,v in sorted(g.items(),key=lambda kv:-len(kv[1])):
    d=sorted(x['dur'] for x in v); o=[x['out'] for x in v]
    print(f"{k:32s} {len(v):4d} {stt.median(d):6.1f} {stt.mean(d):6.1f} {d[int(.9*(len(d)-1))]:6.1f} {sum(d):6.0f} {stt.mean(o):8.0f}")
d=sorted(x['dur'] for x in rows); print('ALL',len(rows),round(stt.median(d),2),round(sum(d)))
ga=defaultdict(list)
for r in rows:
    if r['av']: ga[r['av']].append(r)
print('\navoidable steps:')
for k,v in sorted(ga.items(),key=lambda kv:-len(kv[1])):
    print(f"  {k:28s} n={len(v):3d} model_s={sum(x['dur'] for x in v):5.0f}")
print('  total',sum(len(v) for v in ga.values()),'of',len(rows), 'model_s',round(sum(x['dur'] for v in ga.values() for x in v)))
