<script lang="ts">
  // The old dashboard's live numbers: session clock, machine, live game, goal scorecard, latency, story progress.
  import { onMount } from "svelte";
  import { api } from "../lib/api";
  import { clock, hpPct, mmss, num } from "../lib/format";
  import { checkAllLine, eventLine, hours, OP_TEXT, passClass, spark, thermalLine } from "../lib/stats";
  import type { BenchGrade, GoalsPayload, LatencyPayload, LivePayload, Milestones, ProgressPayload } from "../lib/types";
  import Bench from "./Bench.svelte";

  let { pt }: { pt: string } = $props();

  let live = $state.raw<LivePayload | null>(null);
  let lat = $state.raw<LatencyPayload | null>(null);
  let prog = $state.raw<ProgressPayload | null>(null);
  let goals = $state.raw<GoalsPayload | null>(null);
  let bench = $state.raw<BenchGrade | null>(null);
  let error = $state<string | null>(null);
  let frameRev = $state(0);

  function every(ms: number, fn: () => Promise<void>): () => void {
    let stopped = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const run = async () => {
      await fn().catch((e: Error) => (error = e.message));
      if (!stopped) timer = setTimeout(run, ms);
    };
    void run();
    return () => {
      stopped = true;
      clearTimeout(timer);
    };
  }

  onMount(() => {
    api.bench(pt).then((b) => (bench = b.bench), (e: Error) => (error = e.message));
    const stops = [
      every(2000, async () => {
        live = await api.live();
        frameRev += 1;
        error = null;
      }),
      every(3000, async () => void (lat = await api.latency())),
      every(5000, async () => void (prog = await api.progress())),
      every(15000, async () => void (goals = await api.goals())),
    ];
    return () => stops.forEach((s) => s());
  });

  const th = $derived(live?.thermal ? thermalLine(live.thermal, live.now) : null);
  const check = $derived(live?.runner.check_all ? checkAllLine(live.runner.check_all, live.now) : null);
  const shownSessions = $derived(goals ? goals.sessions.slice(-12) : []);
  const offset = $derived(goals ? goals.sessions.length - shownSessions.length : 0);
  const st = $derived(live?.game.status ?? null);
  const L = $derived(lat?.live ?? null);
  const P = $derived(prog?.live ?? null);
  const u = $derived(goals?.unattended ?? null);
</script>

{#snippet milestones(m: Milestones | null)}
  {#if m}
    <div class="ms">
      <b>milestones {m.reached}/{m.total}</b> <span class="faint">(Act {m.act})</span>
      {#each m.items.filter((i) => i.reached) as i (i.label)}
        <span class="chip ok" title={i.t ? clock(i.t) : "before tracking"}>✓ {i.label}{i.session ? ` · ${i.session}` : ""}</span>
      {/each}
      {#if m.next}<span class="chip next">→ next: {m.next}</span>{/if}
    </div>
  {/if}
{/snippet}

{#snippet kpi(label: string, value: string, cls = "")}
  <div class="kpi {cls}"><div class="v">{value}</div><div class="l">{label}</div></div>
{/snippet}

{#if bench}<Bench grade={bench} />{/if}

{#if error && !live}
  <div class="error">{error}</div>
{:else if !live}
  <div class="empty">Loading…</div>
{:else}
  <section class="strip panel" aria-label="Session">
    <span class="chip">session <b>{live.runner.session ?? "–"}</b></span>
    <span class="chip" class:warn={live.clock.phase === "wrapup"} class:bad={live.clock.phase === "over"}>
      clock <b>{live.clock.left_s === null ? live.clock.phase : mmss(live.clock.left_s)}</b>
      {live.clock.source ? `· ${live.clock.source}` : ""}
    </span>
    {#if th}<span class="chip" class:bad={th.hot} class:stale={th.stale} title={live.thermal?.line}>{th.text}</span>{/if}
    {#if check}<span class="chip {check.level}">{check.text}</span>{/if}
    {#each live.runner.check_all?.fails ?? [] as f (f)}<span class="chip bad">FAIL {f}</span>{/each}
  </section>

  <div class="grid">
    <section class="panel">
      <h2>Live game <span class="right faint">{live.game.reachable ? clock(live.game.t ?? null) : ""}</span></h2>
      {#if !live.game.reachable}
        <div class="empty">{live.game.error}</div>
      {:else if !st}
        <div class="empty">{live.game.note}</div>
      {:else}
        <div class="body">
          <div class="where"><b>{st.region ?? "?"}</b>{st.subregion ? ` · ${st.subregion}` : ""}
            <span class="mode" class:combat={st.mode === "combat"}>{st.mode}</span>
            {#if st.gold !== null}<span class="faint">{st.gold} gold</span>{/if}</div>
          <ul class="party">
            {#each st.party as m (m.name)}
              <li class:dead={m.dead || m.downed}>
                <span class="name">{m.active ? "★ " : ""}{m.name}</span> <span class="faint">{m.classes}</span>
                {#if m.max_hp}
                  <div class="bar"><div class:low={hpPct(m.hp ?? 0, m.max_hp) < 35} style="width: {hpPct(m.hp ?? 0, m.max_hp)}%"></div></div>
                  <span class="faint hp">HP {m.hp}/{m.max_hp}{m.statuses.length ? ` · ${m.statuses.join(", ")}` : ""}</span>
                {/if}
              </li>
            {/each}
          </ul>
        </div>
      {/if}
      <h2>Game events <span class="right faint">since the page opened</span></h2>
      {#if live.events.length}
        <ul class="events">{#each live.events.slice().reverse() as e, i (i)}<li><span class="faint">{clock(e.t)}</span> {e.text}</li>{/each}</ul>
      {:else}<div class="empty">none yet</div>{/if}
    </section>

    <section class="panel">
      <h2>Screen <span class="right faint">{live.screen.note ?? live.screen.error ?? (live.screen.t ? clock(live.screen.t) : "")}</span></h2>
      {#if live.screen.enabled && live.screen.t}
        <img class="shot" src={`/api/v1/live/screen?rev=${frameRev}`} alt="The game screen" />
      {:else}
        <div class="empty">{live.screen.enabled ? "no frame yet" : "screen capture is off (start the server with --screen)"}</div>
      {/if}
    </section>
  </div>

  <section class="panel">
    <h2>Goal scorecard <span class="right faint">per session · green = pass · trend left→right</span></h2>
    {#if !goals || !shownSessions.length}
      <div class="empty">No sessions yet.</div>
    {:else}
      <div class="scroll">
        <table class="goals">
          <thead><tr><th>goal</th><th>target</th>
            {#each shownSessions as s (s.session)}<th class:live={s.live} class:short={s.short} title={num(s.minutes, 1, " min")}>{s.session}{s.live ? " ●" : ""}</th>{/each}
            <th>trend</th><th>best</th><th>pass rate</th></tr></thead>
          <tbody>
            {#each goals.goals as g (g.key)}
              {@const sp = spark(g.series.slice(offset), g.target, g.op)}
              <tr><td>{g.label}</td><td class="faint">{OP_TEXT[g.op]} {g.target}{g.unit}</td>
                {#each shownSessions as s (s.session)}
                  <td class={s.short ? "short" : passClass(s.pass[g.key])}>{num(s.values[g.key], g.digits)}</td>
                {/each}
                <td>{#if sp}<svg width="90" height="20" viewBox="0 0 90 20" aria-hidden="true">
                  <line x1="0" x2="90" y1={sp.targetY} y2={sp.targetY} class="tline" />
                  <polyline points={sp.points} class="sline" />
                  <circle cx={sp.last.x} cy={sp.last.y} r="2.5" class={sp.last.ok ? "dok" : "dbad"} /></svg>{/if}</td>
                <td>{num(g.best, g.digits)}</td><td>{g.pass_rate == null ? "–" : `${g.pass_rate}%`}</td></tr>
            {/each}
            <tr class="sum"><td>passed</td><td></td>{#each shownSessions as s (s.session)}<td>{s.judged ? `${s.passed}/${s.judged}` : "–"}</td>{/each}<td colspan="3"></td></tr>
            <tr class="sum"><td>minutes · points</td><td></td>{#each shownSessions as s (s.session)}<td>{num(s.minutes, 0)} · {s.points ?? "–"}</td>{/each}<td colspan="3"></td></tr>
          </tbody>
        </table>
      </div>
      <div class="body">
        {@render milestones(goals.milestones)}
        {#if u}
          <div class="ms">
            {#if u.alert}<div class="bad"><b>⚠ {clock(u.alert.t)} {u.alert.msg ?? u.alert.kind ?? "alert"}</b></div>{/if}
            <b>unattended {u.live ? `${hours(u.current_h)} now` : "– (runner not running)"}</b>
            <span class="faint">longest {hours(u.longest_h)} · total {hours(u.total_h)} over {u.runs} run{u.runs === 1 ? "" : "s"}</span>
            <span class="chip">{u.fault_total} fault{u.fault_total === 1 ? "" : "s"}</span>
            <span class="chip" class:ok={u.recovered > 0}>{u.recovered} recovered{u.recovery_s_median != null ? ` (median ${u.recovery_s_median} s)` : ""}</span>
            {#if u.failed}<span class="chip next">{u.failed} recovery failed</span>{/if}
            {#if u.steam_offline === false}<span class="chip next">⚠ STEAM NOT OFFLINE</span>{/if}
            {#if !u.live && u.last_stop}<span class="chip">last stop: {u.last_stop}</span>{/if}
            {#each u.events.slice().reverse() as e, i (i)}
              {@const ev = eventLine(e)}
              <div class="ev {ev.level}"><span class="faint">{clock(e.t)}</span> {ev.text}</div>
            {/each}
          </div>
        {/if}
      </div>
    {/if}
  </section>

  <div class="grid">
    <section class="panel">
      <h2>Latency <span class="right faint">{Object.values(lat?.files ?? {}).filter(Boolean).join(" · ")}</span></h2>
      {#if L}
        <div class="kpis">
          {@render kpi(L.in_dialogue ? "idle now (dialogue)" : L.in_combat ? "idle now (combat)" : "idle now",
            L.busy_now ? "acting" : num(L.current_idle_s, 0, "s"), L.busy_now ? "pass" : (L.current_idle_s ?? 0) > 10 && !L.in_dialogue ? "fail" : "")}
          {@render kpi(`idle median explore (<${lat?.targets.median_idle_explore_s}s)`, num(L.idle_explore_median_s, 1, "s"), passClass(L.targets?.median_idle_explore_s?.pass))}
          {@render kpi("idle median / p90", `${num(L.idle_median_s, 1)}/${num(L.idle_p90_s, 0, "s")}`)}
          {@render kpi("gaps >10s (non-dialogue)", num(L.targets?.long_gaps_non_dialogue?.value, 0), passClass(L.targets?.long_gaps_non_dialogue?.pass))}
          {@render kpi("think median", num(L.think_median_s, 1, "s"))}
          {@render kpi("API s/turn · ms/out-tok", `${num(L.api_s_per_turn, 1, "s")} · ${num(L.ms_per_out_tok, 0)}`)}
          {@render kpi(`read round trip (<${lat?.targets.bridge_rt_median_ms}ms)`, num(L.rt_read_median_ms, 0, "ms"), passClass(L.targets?.bridge_rt_median_ms?.pass))}
          {@render kpi("actions / min", num(L.apm, 1))}
          {@render kpi("busy", num(L.busy_pct, 0, "%"))}
          {@render kpi("pipelined", num(L.pipelining_pct, 0, "%"))}
          {@render kpi("think overlap", num(L.think_overlap_pct, 0, "%"))}
        </div>
      {:else}<div class="empty">No live session metrics.</div>{/if}
      {#if lat?.history.length}
        <div class="scroll"><table>
          <thead><tr><th>session</th><th>min</th><th>idle med</th><th>p90</th><th>explore</th><th>&gt;10s</th><th>think</th><th>rt ms</th><th>apm</th><th>busy</th><th>pipe</th><th>overlap</th></tr></thead>
          <tbody>{#each lat.history.slice().reverse() as x (x.session)}
            <tr><td>{x.session}</td><td>{num(x.minutes, 0)}</td><td>{num(x.idle_median_s, 1)}</td><td>{num(x.idle_p90_s, 0)}</td>
              <td class={passClass(x.pass?.median_idle_explore_s)}>{num(x.idle_explore_median_s, 1)}</td>
              <td class={passClass(x.pass?.long_gaps_non_dialogue)}>{x.long_gaps ?? "–"}</td><td>{num(x.think_median_s, 1)}</td>
              <td class={passClass(x.pass?.bridge_rt_median_ms)}>{num(x.rt_read_median_ms, 0)}</td><td>{num(x.apm, 1)}</td>
              <td>{num(x.busy_pct, 0, "%")}</td><td>{num(x.pipelining_pct, 0, "%")}</td><td>{num(x.think_overlap_pct, 0, "%")}</td></tr>
          {/each}</tbody></table></div>
      {/if}
    </section>

    <section class="panel">
      <h2>Story progress <span class="right faint">{prog ? `stuck after ${prog.stuck_after_min} min` : ""}</span></h2>
      {#if P}
        <div class="kpis">
          {@render kpi(`points (${P.session ?? "?"}${P.running ? "" : ", ended"})`, String(P.points))}
          {@render kpi("points / hour", num(P.per_hour, 0))}
          {@render kpi("progress events", String(P.events))}
          {@render kpi(P.stuck ? "STUCK for" : "since last progress", num(P.stuck ? P.stuck_min : P.since_progress_min, 0, "m"), P.stuck ? "fail" : "")}
          {@render kpi("session", num(P.minutes, 0, "m"))}
        </div>
        <ul class="events">{#each (P.recent ?? []).slice().reverse() as e, i (i)}
          <li title={e.what}><span class="faint">{clock(e.t)}</span> <b>+{e.points}</b> <span class="faint">{e.kind}</span> {e.what}</li>
        {:else}<li class="faint">no progress events this session yet</li>{/each}</ul>
      {:else}<div class="empty">No running session.</div>{/if}
      <div class="body">{@render milestones(prog?.milestones ?? null)}</div>
      {#if prog?.history.length}
        <div class="scroll"><table>
          <thead><tr><th>session</th><th>min</th><th>points</th><th>/hour</th><th>events</th><th>longest dry</th><th>stuck</th></tr></thead>
          <tbody>{#each prog.history.slice().reverse() as x (x.session)}
            <tr><td>{x.session}</td><td>{num(x.minutes, 0)}</td><td>{x.points ?? "–"}</td><td>{num(x.per_hour, 0)}</td><td>{x.events ?? "–"}</td>
              <td>{num(x.longest_no_progress_min, 0, "m")}</td>
              <td class={x.stuck_episodes ? "fail" : "pass"}>{x.stuck_episodes ? `${x.stuck_episodes}× ${num(x.stuck_min, 0, "m")}` : "0"}</td></tr>
          {/each}</tbody></table></div>
      {/if}
    </section>
  </div>
{/if}

<style>
  .strip { display: flex; flex-wrap: wrap; gap: 6px; padding: 8px; margin-bottom: 12px; }
  .grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; margin-bottom: 12px; align-items: start; }
  section.panel:not(.strip) { margin-bottom: 12px; }
  .grid > section.panel { margin-bottom: 0; }
  .chip.warn b, .chip.warn { color: var(--warn); }
  .chip.bad, .bad { color: var(--bad); }
  .chip.ok { color: var(--ok); }
  .chip.next { color: var(--warn); }
  .chip.stale { opacity: 0.6; }
  .where { display: flex; flex-wrap: wrap; gap: 6px 10px; align-items: center; }
  .mode { font-size: 10.5px; font-weight: 700; letter-spacing: 0.06em; padding: 1px 6px; border-radius: 4px;
    background: var(--panel-2); color: var(--ok); text-transform: uppercase; }
  .mode.combat { color: var(--bad); }
  .party { list-style: none; margin: 6px 0 0; padding: 0; }
  .party li { padding: 4px 0; }
  .party .name { font-weight: 600; }
  .party li.dead .name { color: var(--bad); }
  .hp { font: 12px var(--mono); }
  .bar { height: 6px; background: var(--hp-bg); border-radius: 3px; overflow: hidden; margin: 3px 0 1px; }
  .bar div { height: 100%; background: var(--ok); }
  .bar div.low { background: var(--bad); }
  .events { list-style: none; margin: 0; padding: 6px 12px 10px; max-height: 240px; overflow-y: auto; font-size: 13px; }
  .events li { padding: 2px 0; border-bottom: 1px dashed var(--line); overflow-wrap: anywhere; }
  .shot { display: block; width: 100%; height: auto; background: #000; border-radius: 0 0 8px 8px; }
  .scroll { overflow-x: auto; }
  table { border-collapse: collapse; font-size: 12.5px; width: 100%; }
  th, td { padding: 4px 8px; text-align: right; white-space: nowrap; border-bottom: 1px solid var(--line); }
  th:first-child, td:first-child { text-align: left; }
  th { color: var(--muted); font-weight: 600; font-size: 11.5px; }
  th.live { color: var(--accent); }
  td.pass { color: var(--ok); }
  td.fail { color: var(--bad); }
  td.short, th.short { color: var(--faint); }
  tr.sum td { color: var(--muted); }
  .tline { stroke: var(--faint); stroke-dasharray: 2 3; }
  .sline { fill: none; stroke: var(--muted); stroke-width: 1.5; }
  .dok { fill: var(--ok); }
  .dbad { fill: var(--bad); }
  .kpis { display: grid; grid-template-columns: repeat(auto-fill, minmax(130px, 1fr)); gap: 8px; padding: 10px 12px; }
  .kpi { background: var(--panel-2); border-radius: 6px; padding: 6px 8px; }
  .kpi .v { font: 600 16px var(--mono); }
  .kpi .l { font-size: 11px; color: var(--muted); }
  .kpi.pass .v { color: var(--ok); }
  .kpi.fail .v { color: var(--bad); }
  .ms { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; margin: 4px 0; font-size: 13px; }
  .ev { width: 100%; font-size: 12.5px; }
  .ev.ok { color: var(--ok); }
  .ev.bad { color: var(--bad); }
  @media (max-width: 900px) {
    .grid { grid-template-columns: minmax(0, 1fr); }
  }
</style>
