<script lang="ts">
  import { onMount } from "svelte";
  import { api, openStream } from "../lib/api";
  import { applyPosition, emptyPositions } from "../lib/breadcrumb";
  import { argLine, clock, hpPct, mmss, num, usd } from "../lib/format";
  import type { PositionRow, Positions } from "../lib/types";
  import { applyEvent, fromSnapshot, says, secondsLeft, type WatchState } from "../lib/watch";
  import Breadcrumb from "./Breadcrumb.svelte";

  let { pt }: { pt: string } = $props();

  let s = $state.raw<WatchState | null>(null);
  let error = $state<string | null>(null);
  let connected = $state(false);
  let now = $state(Date.now() / 1000);
  let shotOk = $state(true);
  let filter = $state<"all" | "act" | "read">("all");
  let pos = $state.raw<Positions>(emptyPositions());
  let liveVideo = $state(false);
  let close: (() => void) | null = null;

  async function load() {
    close?.();
    try {
      const snap = await api.watch(pt);
      s = fromSnapshot(snap);
      error = null;
      api.positions(pt).then((p) => (pos = p), () => (pos = emptyPositions()));
      api.recorder().then((r) => (liveVideo = Boolean(r.recorder?.recording)), () => (liveVideo = false));
      close = openStream(
        snap.stream_id,
        (type, data) => {
          if (type === "position") {
            const row = data as PositionRow;
            if (row.playthrough === pt) pos = applyPosition(pos, row);
          } else if (s) s = applyEvent(s, type, data);
        },
        () => void load(),
        (open) => (connected = open),
      );
    } catch (e) {
      error = (e as Error).message;
    }
  }

  onMount(() => {
    void load();
    const tick = setInterval(() => (now = Date.now() / 1000), 1000);
    return () => {
      clearInterval(tick);
      close?.();
    };
  });

  const left = $derived(s ? secondsLeft(s.runner, now) : null);
  const shotSrc = $derived(liveVideo ? "/api/v1/screen.mjpg" : s ? `/api/v1/screenshot?rev=${s.screenshotRev}` : "");
  const calls = $derived(
    s ? s.actions.filter((a) => filter === "all" || (filter === "read" ? a.kind === "read" : a.kind !== "read")).slice(-60).reverse() : [],
  );
  const words = $derived(s ? says(s, 6) : []);
</script>

{#if error}
  <div class="error">{error}</div>
{:else if !s}
  <div class="empty">Loading…</div>
{:else}
  <section class="strip panel" aria-label="Session">
    <span class="chip"><span class="dot" class:on={connected} class:off={!connected}></span>{connected ? "connected" : "offline"}</span>
    <span class="chip">session <b>{s.session ?? "–"}</b></span>
    {#if s.runner?.model}<span class="chip">model <b>{s.runner.model}</b>{s.runner.effort ? ` · ${s.runner.effort}` : ""}</span>{/if}
    <span class="chip" class:warn={left !== null && left < 600}>clock <b>{left === null ? "ended" : mmss(left)}</b></span>
    <span class="chip">cost <b>{usd(s.cost.total_usd)}</b> · last {usd(s.cost.last_session_usd)}</span>
    {#if s.latency}
      <span class="chip">idle p50 <b>{num(s.latency.idle_median_s, 1, " s")}</b></span>
      <span class="chip">APM <b>{num(s.latency.apm, 1)}</b></span>
      <span class="chip">busy <b>{num(s.latency.busy_pct, 0, "%")}</b></span>
      <span class="chip">think overlap <b>{num(s.latency.think_overlap_pct, 0, "%")}</b></span>
      <span class="chip">round trip <b>{num(s.latency.bridge_rt_ms, 0, " ms")}</b></span>
    {/if}
  </section>

  <div class="grid">
    <div class="col main">
      <section class="panel screen o1">
        <h2>Game <span class="right faint">for viewers only, never sent to the agent</span></h2>
        {#if shotOk}
          <img src={shotSrc} alt="Latest game capture" onerror={() => (shotOk = false)} onload={() => (shotOk = true)} />
        {:else}
          <div class="empty">No capture yet.</div>
        {/if}
      </section>

      <section class="panel o4">
        <h2>Agent says</h2>
        {#if words.length}
          <ol class="says">
            {#each words as w, i (i)}
              <li class:latest={i === 0}><span class="t faint">{w.clock}</span> {w.text}</li>
            {/each}
          </ol>
        {:else}
          <div class="empty">Nothing said yet.</div>
        {/if}
      </section>

      <section class="panel o5">
        <h2>
          Tool calls
          <span class="right filters">
            {#each ["all", "act", "read"] as f (f)}
              <button class:on={filter === f} onclick={() => (filter = f as typeof filter)}>{f}</button>
            {/each}
          </span>
        </h2>
        {#if calls.length}
          <ul class="calls">
            {#each calls as a, i (i)}
              <li class:fail={!a.ok}>
                <span class="t faint">{clock(a.t)}</span>
                <span class="tool k-{a.kind}">{a.tool.replace("mcp__bg3__", "")}</span>
                <span class="args muted">{argLine(a.args)}</span>
                <span class="ms faint">{a.ms ?? "–"} ms</span>
                <details><summary class="faint">result</summary><pre>{a.result}</pre></details>
              </li>
            {/each}
          </ul>
        {:else}
          <div class="empty">{s.live ? "No tool calls yet." : "Tool calls show while this playthrough runs."}</div>
        {/if}
      </section>
    </div>

    <div class="col side">
      <section class="panel now o2">
        <h2>Now</h2>
        <div class="body">
          <div class="label faint">Goal</div>
          <div class="goal">{s.goal ?? "–"}</div>
          {#if s.party}
            <div class="where">
              <span>{s.party.where ?? "?"}</span>
              {#if s.party.mode}<span class="mode" class:combat={s.party.mode.startsWith("COMBAT")} title={s.party.mode}>{s.party.mode.split(" ")[0]}</span>{/if}
              {#if s.party.gold !== null}<span class="faint">gold {s.party.gold}</span>{/if}
            </div>
          {/if}
        </div>
      </section>

      <section class="panel o3">
        <h2>Party <span class="right faint">{s.party ? `from ${s.party.source}` : ""}</span></h2>
        {#if s.party?.party.length}
          <ul class="party">
            {#each s.party.party as m (m.name)}
              <li class:dead={m.dead} class:downed={m.downed}>
                <div class="row">
                  <span class="name">{m.active ? "★ " : ""}{m.name}</span>
                  <span class="cls faint">{m.classes}</span>
                  <span class="hp">{m.hp}/{m.max_hp}{m.temp_hp ? `+${m.temp_hp}` : ""}</span>
                </div>
                <div class="bar"><div style="width: {hpPct(m.hp, m.max_hp)}%" class:low={hpPct(m.hp, m.max_hp) < 35}></div></div>
                {#if m.statuses.length || m.dead || m.downed}
                  <div class="st faint">{[m.dead ? "dead" : m.downed ? "downed" : "", ...m.statuses].filter(Boolean).join(" · ")}</div>
                {/if}
              </li>
            {/each}
          </ul>
        {:else}
          <div class="empty">No party status yet.</div>
        {/if}
      </section>

      <Breadcrumb {pos} onLevel={(lv) => api.positions(pt, lv).then((p) => (pos = p), () => {})} />

      {#if s.memory.length}
        <section class="panel o7">
          <h2>Memory edits</h2>
          <ul class="mem">
            {#each s.memory as c (c.sha)}
              <li><span class="faint">{clock(c.t)}</span> {c.subject}</li>
            {/each}
          </ul>
        </section>
      {/if}
    </div>
  </div>
{/if}

<style>
  .strip { display: flex; flex-wrap: wrap; gap: 6px; padding: 8px; margin-bottom: 12px; }
  .chip.warn b { color: var(--warn); }
  .grid { display: grid; grid-template-columns: minmax(0, 1fr) 340px; gap: 12px; align-items: start; }
  .col { display: flex; flex-direction: column; gap: 12px; min-width: 0; }
  .screen img { display: block; width: 100%; height: auto; border-radius: 0 0 8px 8px; background: #000; }
  .says { list-style: none; margin: 0; padding: 6px 12px 10px; }
  .says li { padding: 5px 0; border-bottom: 1px dashed var(--line); color: var(--muted); white-space: pre-wrap; }
  .says li:last-child { border-bottom: 0; }
  .says li.latest { color: var(--text); font-size: 15px; }
  .t { font: 11px var(--mono); }
  .filters button {
    background: none; border: 1px solid var(--line); border-radius: 4px; padding: 0 6px; cursor: pointer;
    color: var(--muted); font-size: 11px;
  }
  .filters button.on { color: var(--text); border-color: var(--accent); }
  .calls { list-style: none; margin: 0; padding: 0; max-height: 520px; overflow-y: auto; }
  .calls li {
    display: grid; grid-template-columns: 64px auto minmax(0, 1fr) auto; gap: 2px 8px; align-items: baseline;
    padding: 4px 12px; border-bottom: 1px solid var(--line); font-size: 13px;
  }
  .calls li.fail .tool { color: var(--bad); }
  .tool { font: 600 12.5px var(--mono); }
  .k-read { color: var(--read); } .k-act { color: var(--act); } .k-vision { color: var(--vision); } .k-save { color: var(--save); }
  .args { font: 12px var(--mono); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .ms { font: 11px var(--mono); text-align: right; }
  .calls details { grid-column: 2 / -1; }
  .calls summary { font-size: 11px; cursor: pointer; }
  .calls pre { margin: 4px 0; white-space: pre-wrap; word-break: break-word; max-height: 240px; overflow: auto; }
  .label { font-size: 11px; text-transform: uppercase; letter-spacing: 0.06em; }
  .goal { font-size: 15px; font-weight: 550; margin: 2px 0 8px; }
  .where { display: flex; flex-wrap: wrap; gap: 6px 10px; align-items: center; }
  .mode {
    font-size: 10.5px; font-weight: 700; letter-spacing: 0.06em; padding: 1px 6px; border-radius: 4px;
    background: var(--panel-2); color: var(--ok); text-transform: uppercase;
  }
  .mode.combat { color: var(--bad); }
  .party { list-style: none; margin: 0; padding: 4px 12px 10px; }
  .party li { padding: 6px 0; }
  .party .row { display: flex; gap: 8px; align-items: baseline; }
  .party .name { font-weight: 600; }
  .party .cls { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 12px; }
  .party .hp { font: 12px var(--mono); }
  .bar { height: 6px; background: var(--hp-bg); border-radius: 3px; overflow: hidden; margin-top: 3px; }
  .bar div { height: 100%; background: var(--ok); }
  .bar div.low { background: var(--bad); }
  .party li.dead .name, .party li.downed .name { color: var(--bad); }
  .st { font-size: 11.5px; margin-top: 2px; }
  .mem { list-style: none; margin: 0; padding: 6px 12px 10px; font-size: 13px; }
  @media (max-width: 900px) {
    /* one column: the picture, then what it is doing and the party, then its words and calls */
    .grid { display: flex; flex-direction: column; align-items: stretch; }
    .col { display: contents; }
    .o1 { order: 1; } .o2 { order: 2; } .o3 { order: 3; } .o4 { order: 4; } .o5 { order: 5; } :global(.o6) { order: 6; } .o7 { order: 7; }
  }
  @media (max-width: 560px) {
    .calls li { grid-template-columns: auto minmax(0, 1fr) auto; }
    .calls li .t { display: none; }
    .calls details { grid-column: 1 / -1; }
  }
</style>
