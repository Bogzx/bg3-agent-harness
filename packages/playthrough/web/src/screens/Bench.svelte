<script lang="ts">
  // A benchmark run's grade (bg3bench's manifest.json + result.json): verdict, progress curve, gold by method,
  // choices, luck, cost and time. `compact` keeps the verdict line and the numbers (Replay).
  import { usd } from "../lib/format";
  import { counters, curve, dur, goldRows, luckLine, thinkPct, tokens, verdict } from "../lib/bench";
  import type { BenchGrade } from "../lib/types";

  let { grade, compact = false }: { grade: BenchGrade; compact?: boolean } = $props();

  const W = 320;
  const H = 100;
  const PLOT_H = H - 16; // the time labels go below the plot
  const ok = $derived("result" in grade ? grade : null);
  const r = $derived(ok?.result ?? null);
  const m = $derived(ok?.manifest ?? null);
  const v = $derived(r ? verdict(r) : null);
  const c = $derived(r?.grade ? curve(r.grade.curve.points, W, PLOT_H, 8) : null);
  const think = $derived(r ? thinkPct(r) : null);
</script>

{#snippet kpi(label: string, value: string, title = "")}
  <div class="kpi" {title}><div class="v">{value}</div><div class="l">{label}</div></div>
{/snippet}

<section class="panel bench" aria-label="Benchmark grade">
  {#if "error" in grade}
    <h2>Benchmark grade</h2>
    <div class="error">Can't read this run's bench files: {grade.error}</div>
  {:else if r && m && v}
    <h2>
      Benchmark grade
      <span class="right faint">{m.scenario.name} · {m.series} run {m.run} · {m.model.slug}{m.model.reasoning_effort ? ` (${m.model.reasoning_effort})` : ""} via {m.client.name}</span>
    </h2>
    <div class="head">
      <span class="verdict {v.level}">{v.text}</span>
      {#if r.t_pass_s !== null}<span>goal reached at <b>{dur(r.t_pass_s)}</b></span>{/if}
      {#if r.grade?.progress != null}<span>progress <b>{Math.round(r.grade.progress * 100)}%</b></span>{/if}
      {#if r.grade?.violations.length}<span class="bad">{r.grade.violations.length} rule violation{r.grade.violations.length === 1 ? "" : "s"}</span>{/if}
      {#if r.fairness.leak || r.fairness.check}<span class="bad">fairness: {r.fairness.leak} leak · {r.fairness.check} check</span>{/if}
    </div>
    <div class="kpis">
      {@render kpi("cost", usd(r.cost_usd))}
      {@render kpi("tokens in · out", `${tokens(r.tokens.input)} · ${tokens(r.tokens.output)}`,
        `cache read ${tokens(r.tokens.cache_read)} · cache write ${tokens(r.tokens.cache_write)}`)}
      {@render kpi("wall clock", dur(r.wall_s))}
      {@render kpi(think === null ? "thinking" : `thinking (${think}% of wall)`, dur(r.think_s), "the model's own time")}
      {@render kpi("in game", dur(r.play_s))}
      {@render kpi("luck", r.luck ? `z ${r.luck.z.toFixed(2)}` : "–", luckLine(r.luck))}
      {#if !compact}
        {@render kpi("cache read · write", `${tokens(r.tokens.cache_read)} · ${tokens(r.tokens.cache_write)}`)}
        {@render kpi("fights · downs · deaths", `${r.deaths.fights} · ${r.deaths.downs} · ${r.deaths.deaths}`)}
        {@render kpi("wipes · reloads", `${r.deaths.wipes} · ${r.reloads}`)}
      {/if}
    </div>
    {#if think !== null && r.wall_s}
      <div class="split" title="thinking {dur(r.think_s)} of {dur(r.wall_s)} wall clock">
        <div class="think" style="width: {Math.min(100, think)}%"></div>
      </div>
      <div class="legend faint"><span class="sw think"></span> thinking <span class="sw wall"></span> rest of the wall clock</div>
    {/if}

    {#if !compact}
      <div class="parts">
        <div class="part">
          <h3>Progress <span class="faint">{r.grade ? r.grade.curve.fact : ""}</span></h3>
          {#if c}
            <svg viewBox="0 0 {W} {H}" class="curve" role="img" aria-label="Progress curve of {r.grade?.curve.fact}">
              <line x1="8" x2={W - 8} y1={PLOT_H - 8} y2={PLOT_H - 8} class="axis" />
              <polyline points={c.line} class="line" />
              {#each c.dots as d (d.at_s)}
                <circle cx={d.x} cy={d.y} r="3.5" class="dot"><title>{dur(d.at_s)}: {d.value}{d.peak !== null && d.peak !== d.value ? ` (peak ${d.peak})` : ""}</title></circle>
                <text x={d.x} y={d.y < 20 ? d.y + 16 : d.y - 7} dx={d.y < 20 ? -9 : 0} class="lbl">{d.value}</text>
              {/each}
              {#each c.ticks as k, i (k.at_s)}
                <text x={k.x} y={H - 3} class="tick" class:missed={!k.reached}
                      text-anchor={i === c.ticks.length - 1 && k.x > W - 30 ? "end" : "middle"}>{dur(k.at_s)}{k.reached ? "" : " –"}</text>
              {/each}
            </svg>
          {:else}
            <div class="empty">{r.grade ? "No checkpoint reached." : "Graded by the runner's milestone (no curve)."}</div>
          {/if}
        </div>

        {#if r.methods}
          <div class="part">
            <h3>Gold by method <span class="faint">reported, not graded</span></h3>
            <table class="methods">
              <tbody>
                {#each goldRows(r.methods) as g (g.method)}
                  <tr class:zero={!g.gold}><td>{g.method}</td><td class="n">{g.gold}</td>
                    <td class="barcell"><div class="bar"><div style="width: {g.pct}%"></div></div></td></tr>
                {/each}
              </tbody>
            </table>
            <div class="chips">
              {#each counters(r.methods) as x (x)}<span class="chip">{x}</span>{:else}<span class="faint">no crimes, kills or trades</span>{/each}
            </div>
          </div>
        {/if}

        {#if r.choices}
          <div class="part">
            <h3>Choices picked
              {#if r.choices.fit}<span class="faint">persona fit {r.choices.fit.status === "ok" ? `${r.choices.fit.score}/10` : r.choices.fit.status.replace("_", " ")}</span>{/if}
            </h3>
            {#if r.choices.fit?.summary}<p class="faint fit">{r.choices.fit.summary}</p>{/if}
            <ul class="picks">
              {#each r.choices.picks as p, i (i)}
                <li><b>{p.scene}</b> → {p.pick}
                  {#if p.options.length > 1}<span class="faint"> (of {p.options.join(" / ")})</span>{/if}
                  {#if p.reason}<div class="faint">{p.reason}</div>{/if}</li>
              {:else}<li class="faint">no choice events</li>{/each}
            </ul>
          </div>
        {/if}
      </div>
    {/if}
  {/if}
</section>

<style>
  .bench { margin-bottom: 12px; }
  .head { display: flex; flex-wrap: wrap; align-items: baseline; gap: 6px 14px; padding: 10px 12px 0; font-size: 13.5px; }
  .verdict { font: 700 13px var(--mono); text-transform: uppercase; letter-spacing: 0.05em; padding: 2px 8px; border-radius: 4px;
    background: var(--panel-2); }
  .verdict.ok { color: var(--ok); } .verdict.bad, .bad { color: var(--bad); } .verdict.warn { color: var(--warn); }
  .kpis { display: grid; grid-template-columns: repeat(auto-fill, minmax(130px, 1fr)); gap: 8px; padding: 10px 12px; }
  .kpi { background: var(--panel-2); border-radius: 6px; padding: 6px 8px; }
  .kpi .v { font: 600 14px var(--mono); white-space: nowrap; }
  .kpi .l { font-size: 11px; color: var(--muted); }
  .split { margin: 0 12px; height: 8px; border-radius: 4px; background: var(--line); overflow: hidden; }
  .split .think { height: 100%; background: var(--accent-2); }
  .legend { padding: 4px 12px 10px; font-size: 11.5px; display: flex; gap: 6px; align-items: center; }
  .sw { display: inline-block; width: 10px; height: 8px; border-radius: 2px; }
  .sw.think { background: var(--accent-2); } .sw.wall { background: var(--line); margin-left: 8px; }
  .parts { display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 12px; padding: 0 12px 12px; }
  .part { background: var(--panel-2); border-radius: 6px; padding: 8px 10px; min-width: 0; }
  h3 { margin: 0 0 6px; font-size: 13px; display: flex; gap: 8px; align-items: baseline; flex-wrap: wrap; }
  h3 .faint { font-weight: 400; font-size: 11.5px; }
  .curve { display: block; width: 100%; max-width: 440px; }
  .axis { stroke: var(--line); }
  .line { fill: none; stroke: var(--accent); stroke-width: 2; }
  .dot { fill: var(--accent); }
  .lbl { font: 11px var(--mono); fill: var(--muted); text-anchor: middle; }
  .tick { font: 10px var(--mono); fill: var(--muted); }
  .tick.missed { fill: var(--faint); }
  .methods { width: 100%; border-collapse: collapse; font-size: 12.5px; }
  .methods td { padding: 2px 4px; }
  .methods tr.zero { color: var(--faint); }
  .methods .n { text-align: right; font-family: var(--mono); width: 3em; }
  .barcell { width: 55%; }
  .bar { height: 6px; background: var(--line); border-radius: 3px; overflow: hidden; }
  .bar div { height: 100%; background: var(--ok); }
  .chips { display: flex; flex-wrap: wrap; gap: 4px; margin-top: 6px; font-size: 12px; }
  .picks { list-style: none; margin: 0; padding: 0; font-size: 13px; }
  .picks li { padding: 3px 0; border-bottom: 1px dashed var(--line); overflow-wrap: anywhere; }
  .fit { margin: 0 0 4px; font-size: 12px; }
</style>
