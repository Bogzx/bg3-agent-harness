#!/usr/bin/env python3
"""Where the long gaps of a session went: per gap > TH s, the model turns, tool calls by category, output and
thinking tokens; then a 0.1 s attribution of all that time to party busy / tool exec / model -> next tool.
Usage: python3 tools/dev/gap_causes.py runtime/sessions/<pt> S009 [5]"""
import json, sys, datetime
from collections import Counter, defaultdict
sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parents[2] / 'bridge/src'))
from bg3ai import metrics as M
D = sys.argv[1]; S = sys.argv[2]; TH = float(sys.argv[3]) if len(sys.argv) > 3 else 5.0
recs = M.read_jsonl(f'{D}/{S}.metrics.jsonl')
tools = [r for r in recs if r.get('type') == 'tool']
samples = [r for r in recs if r.get('type') == 'sample']
acts = M.action_intervals(tools)
gl = M.gaps(acts, samples)
ev = [json.loads(l) for l in open(f'{D}/{S}.jsonl')]
def ts(e):
    return datetime.datetime.fromisoformat(e['timestamp'].replace('Z', '+00:00')).timestamp()
# model requests: group assistant events by message id; thinking tokens preceding them
reqs = {}; order = []; pend_think = 0
uses = {}  # tool_use id -> (name, input)
results = {}
for e in ev:
    if e.get('subtype') == 'thinking_tokens':
        pend_think += e.get('estimated_tokens_delta', 0) or 0
    if e['type'] == 'assistant':
        m = e['message']; mid = m['id']
        if mid not in reqs:
            reqs[mid] = dict(t=ts(e), out=0, think=0, tools=[], text=0); order.append(mid)
        r = reqs[mid]; r['think'] += pend_think; pend_think = 0
        r['out'] = max(r['out'], m['usage'].get('output_tokens', 0))
        for c in m['content']:
            if c['type'] == 'tool_use':
                n = c['name'].replace('mcp__bg3__', ''); r['tools'].append(n); uses[c['id']] = (n, c['input'], ts(e))
            elif c['type'] == 'text':
                r['text'] += len(c['text'])
    if e['type'] == 'user':
        for c in e['message'].get('content', []) if isinstance(e['message'].get('content'), list) else []:
            if c.get('type') == 'tool_result':
                results[c['tool_use_id']] = ts(e)
# thinking delta may be cumulative per message; fine as estimate
def cat(n):
    if n in ('Edit', 'Write'): return 'memory-write'
    if n in ('Read', 'Grep', 'Glob'): return 'memory-read'
    if n.startswith('wiki'): return 'wiki'
    if n in M.ACTION_TOOLS or n in M.BATCH_TOOLS or n == 'queue_actions': return 'action'
    if n == 'wait_for': return 'wait_for'
    if n == 'save_game': return 'save'
    return 'read:' + n
# attribute each request's latency: from the previous event (tool result / previous request) to its t
evts = sorted([(r['t'], 'req', mid) for mid, r in reqs.items()] + [(t, 'res', k) for k, t in results.items()])
prev_t = None; lat = {}
for t, k, x in evts:
    if k == 'req' and prev_t is not None: lat[x] = t - prev_t
    prev_t = t
rows = []; share = Counter(); tot = 0
for g in gl:
    if g['s'] <= TH: continue
    a, b = g['t'], g['t'] + g.get('gap_s', g['s'])
    inside = [mid for mid in order if a <= reqs[mid]['t'] <= b + 0.5]
    busy = sum(1 for s in samples if a <= s['t'] <= b and (s.get('moving') or s.get('job')))
    ns = sum(1 for s in samples if a <= s['t'] <= b)
    tc = Counter(); out = th = 0
    for mid in inside:
        r = reqs[mid]; out += r['out']; th += r['think']
        for n in r['tools']: tc[cat(n)] += 1
        c = cat(r['tools'][0]) if r['tools'] else 'text-only'
        share[c] += lat.get(mid, 0)
    # tool execution time inside the gap (non-action)
    texec = 0
    for uid, (n, inp, tu) in uses.items():
        if a <= tu <= b and uid in results and cat(n) != 'action':
            d = results[uid] - tu; texec += d; share['exec:' + cat(n)] += d
    tot += g.get('gap_s', g['s'])
    rows.append((g, len(inside), dict(tc), out, th, busy, ns, texec))
for g, n, tc, out, th, busy, ns, te in rows:
    print(f"{datetime.datetime.fromtimestamp(g['t']).strftime('%H:%M:%S')} {g['s']:5.1f}s {g['kind'][:4]} next={g['next']:<16} turns={n:2d} out={out:5d} think~{th:5d} busy={busy}/{ns} exec={te:4.1f}s {tc}")
print('total gap s >', TH, round(tot, 1), 'n=', len(rows))
for k, v in share.most_common(): print(f'  {k:<28} {v:6.1f}s {100*v/tot:5.1f}%')

# ---- fine attribution (0.1 s steps) of every gap > TH: party busy vs tool exec vs model generating
import bisect
st = sorted(samples, key=lambda s: s['t']); stt = [s['t'] for s in st]
def busy_at(t):
    i = bisect.bisect_left(stt, t)
    if i >= len(st): return False
    s = st[i]  # the sample after t reports movement since the previous sample
    return bool(s.get('moving') or s.get('job')) and s['t'] - t < 1.5
ex = []
for uid, (n, inp, tu) in uses.items():
    if uid in results: ex.append((tu, results[uid], n))
reqt = sorted((reqs[m]['t'], m) for m in order)
def next_req(t):
    i = bisect.bisect_left(reqt, (t, ''))
    return reqs[reqt[i][1]] if i < len(reqt) else None
fine = Counter(); total = 0; first_tool = Counter()
for g, *_ in rows:
    a, b = g['t'], g['t'] + g.get('gap_s', g['s']); t = a
    while t < b:
        total += 0.1
        if busy_at(t): fine['party busy (walking/job)'] += 0.1
        else:
            hit = [n for u0, u1, n in ex if u0 <= t <= u1 and cat(n) != 'action']
            if hit: fine['tool exec: ' + cat(hit[0])] += 0.1
            else:
                r = next_req(t)
                c = (cat(r['tools'][0]) if r and r['tools'] else 'text/none')
                fine['model -> ' + c] += 0.1
        t += 0.1
print('\nFINE attribution of', round(total, 1), 's')
for k, v in fine.most_common(): print(f'  {k:<36} {v:6.1f}s {100*v/total:5.1f}%')

print('\nMEMORY EDITS: time, busy at start, model latency before it')
for uid, (n, inp, tu) in sorted(uses.items(), key=lambda kv: kv[1][2]):
    if cat(n) == 'memory-write':
        print(datetime.datetime.fromtimestamp(tu).strftime('%H:%M:%S'), n, 'busy' if busy_at(tu) else 'IDLE', inp.get('file_path', '')[-40:])
