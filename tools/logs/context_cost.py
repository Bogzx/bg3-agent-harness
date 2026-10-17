#!/usr/bin/env python3
"""Token / context cost of a session from Sxxx.jsonl (Claude stream-json).

Per API call (assistant message id): context = input + cache_read + cache_creation tokens. Reports the context at
start / median / end, its growth, total cache-read tokens, output tokens, cost (result line), compactions, and what
the context is made of: tool-result chars by tool (images counted separately), the start injection (hook output +
system prompt size are not in the stream, estimated from the first call's context).
Usage: python3 tools/logs/context_cost.py claude-run-2 S025 S026 [...]
"""
import json, os, sys, collections

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def short(n):
    return n[len("mcp__bg3__"):] if n.startswith("mcp__bg3__") else n


def main():
    run, sids = sys.argv[1], sys.argv[2:]
    for sid in sids:
        p = os.path.join(ROOT, "runtime/sessions", run, sid + ".jsonl")
        msgs = {}
        order = []
        res_chars = collections.Counter()
        res_n = collections.Counter()
        images = collections.Counter()
        name_of = {}
        cost = out_total = None
        compacts = 0
        hook_chars = 0
        for line in open(p, errors="replace"):
            try:
                d = json.loads(line)
            except ValueError:
                continue
            t = d.get("type")
            if t == "assistant":
                m = d["message"]
                u = m.get("usage") or {}
                if m["id"] not in msgs:
                    order.append(m["id"])
                msgs[m["id"]] = u
                for b in m.get("content") or []:
                    if b.get("type") == "tool_use":
                        name_of[b["id"]] = short(b["name"])
            elif t == "user":
                c = (d.get("message") or {}).get("content")
                if isinstance(c, list):
                    for b in c:
                        if b.get("type") == "tool_result":
                            name = name_of.get(b.get("tool_use_id"), "?")
                            cc = b.get("content")
                            if isinstance(cc, list):
                                for x in cc:
                                    if x.get("type") == "image":
                                        images[name] += 1
                                    else:
                                        res_chars[name] += len(x.get("text", ""))
                            else:
                                res_chars[name] += len(str(cc))
                            res_n[name] += 1
            elif t == "result":
                cost = d.get("total_cost_usd")
                out_total = (d.get("usage") or {}).get("output_tokens")
            elif t == "system" and d.get("subtype") == "compact_boundary":
                compacts += 1
            elif t == "system" and d.get("subtype") == "hook_response":
                hook_chars += len(d.get("output") or "")
        ctx = [u.get("input_tokens", 0) + u.get("cache_read_input_tokens", 0) + u.get("cache_creation_input_tokens", 0)
               for u in (msgs[i] for i in order)]
        # per-message usage in stream-json is the message_start snapshot (output ~1-10 tokens): the result has the total
        out = out_total if out_total is not None else sum(u.get("output_tokens", 0) for u in msgs.values())
        cread = sum(u.get("cache_read_input_tokens", 0) for u in msgs.values())
        ccreate = sum(u.get("cache_creation_input_tokens", 0) for u in msgs.values())
        if not ctx:
            continue
        s = sorted(ctx)
        tot_chars = sum(res_chars.values())
        print(f"== {sid}: {len(ctx)} API calls, context start {ctx[0] / 1000:.0f}k, median {s[len(s) // 2] / 1000:.0f}k, "
              f"end {ctx[-1] / 1000:.0f}k, max {s[-1] / 1000:.0f}k; compactions {compacts}; cache-read {cread / 1e6:.1f}M, "
              f"cache-write {ccreate / 1e3:.0f}k, output {out / 1e3:.0f}k; cost ${cost or 0:.2f}; start hook {hook_chars / 1000:.0f}k chars")
        print(f"   tool results {tot_chars / 1000:.0f}k chars (~{tot_chars / 3.5 / 1000:.0f}k tok), images {sum(images.values())} "
              f"(~{sum(images.values()) * 1.2:.0f}k tok?) — top: "
              + ", ".join(f"{k} {v / 1000:.0f}k/{res_n[k]} ({v / res_n[k]:.0f}/call)" for k, v in res_chars.most_common(10)))


if __name__ == "__main__":
    main()
