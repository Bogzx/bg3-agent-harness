<script lang="ts">
  import { onMount } from "svelte";
  import { api } from "../lib/api";
  import { argLine, clock, date, mmss } from "../lib/format";
  import { marks as benchMarks } from "../lib/bench";
  import { coverage, locate, recent, step, ticks } from "../lib/replay";
  import type { BenchGrade, ReplayPayload } from "../lib/types";
  import Bench from "./Bench.svelte";

  let { pt, session, onSession }: { pt: string; session: string | null; onSession: (s: string) => void } = $props();

  const SPEEDS = [1, 4, 16, 64];
  let sessions = $state<{ session: string; started: number | null; minutes: number | null }[]>([]);
  let data = $state.raw<ReplayPayload | null>(null);
  let bench = $state.raw<BenchGrade | null>(null);
  let error = $state<string | null>(null);
  let t = $state(0);
  let playing = $state(false);
  let speed = $state(1);
  let video = $state<HTMLVideoElement | null>(null);
  let src = $state<string | null>(null); // the loaded segment file

  onMount(() => {
    api.sessions(pt).then(
      (r) => {
        sessions = r.sessions.filter((s) => s.started);
        const pick = session ?? sessions[sessions.length - 1]?.session;
        if (pick && pick !== session) onSession(pick);
        else if (pick) void load(pick);
      },
      (e: Error) => (error = e.message),
    );
    api.bench(pt).then((b) => (bench = b.bench), (e: Error) => (error = e.message));
    const tick = setInterval(() => {
      // the clock runs here only while no video drives it (a gap, or a session without recording)
      if (!playing || !data || (video && src && !video.paused)) return;
      t = Math.min(data.ended, t + 0.25 * speed);
      if (t >= data.ended) playing = false;
      sync();
    }, 250);
    return () => clearInterval(tick);
  });

  async function load(s: string) {
    try {
      const r = await api.replay(pt, s);
      data = r;
      t = r.started;
      error = null;
      sync();
    } catch (e) {
      error = (e as Error).message;
    }
  }

  /** Point the video at the playhead: load the segment that covers t, seek, play or pause. */
  function sync(seek = false) {
    if (!data || !video) return;
    const at = locate(data.segments, t);
    if (!at) {
      if (!video.paused) video.pause();
      src = null;
      return;
    }
    if (src !== at.seg.file) {
      src = at.seg.file;
      video.src = `/api/v1/video/${at.seg.file}`;
      seek = true;
    }
    if (seek) video.currentTime = at.offset;
    video.playbackRate = Math.min(speed, 16);
    if (playing && video.paused) void video.play().catch(() => {});
    if (!playing && !video.paused) video.pause();
  }

  function onVideoTime() {
    if (!data || !video || !src || video.paused) return;
    const seg = data.segments.find((s) => s.file === src);
    if (seg) t = seg.start + video.currentTime;
  }

  function onVideoEnded() {
    const seg = data?.segments.find((s) => s.file === src);
    if (seg) t = (seg.end ?? t) + 0.01;
    sync();
  }

  function seekTo(nt: number) {
    if (!data) return;
    t = Math.max(data.started, Math.min(data.ended, nt));
    sync(true);
  }

  function toggle() {
    playing = !playing;
    sync();
  }

  const shown = $derived(data ? recent(data.events, t, 14) : []);
  const marks = $derived(data ? ticks(data.events, data.started, data.ended) : []);
  const covered = $derived(data ? coverage(data.segments, data.started, data.ended) : []);
  const hasVideo = $derived(Boolean(data?.segments.length));
  const inGap = $derived(data ? !locate(data.segments, t) : true);
  const graded = $derived(bench && "result" in bench ? bench : null);
  const gradeMarks = $derived(data && graded ? benchMarks(graded.manifest, graded.result, data.started, data.ended) : []);
</script>

{#if error}
  <div class="error">{error}</div>
{:else}
  <section class="bar panel">
    <select aria-label="Session" value={session ?? ""} onchange={(e) => onSession((e.currentTarget as HTMLSelectElement).value)}>
      {#each sessions as s (s.session)}<option value={s.session}>{s.session} · {date(s.started)}</option>{/each}
    </select>
    {#if data}
      <button onclick={() => seekTo(step(data!.events, t, -1))} aria-label="Previous event">◀</button>
      <button class="play" onclick={toggle} aria-label={playing ? "Pause" : "Play"}>{playing ? "❚❚" : "▶"}</button>
      <button onclick={() => seekTo(step(data!.events, t, 1))} aria-label="Next event">▶|</button>
      <span class="speeds">
        {#each SPEEDS as sp (sp)}<button class:on={speed === sp} onclick={() => { speed = sp; sync(); }}>{sp}×</button>{/each}
      </span>
      <span class="time">{clock(t)} <span class="faint">· {mmss(Math.round(t - data.started))} / {mmss(Math.round(data.ended - data.started))}</span></span>
    {/if}
  </section>

  {#if bench}<Bench grade={bench} compact />{/if}

  {#if data}
    <div class="scrub" role="presentation">
      {#each covered as [a, b], i (i)}<div class="cov" style="left: {a}%; width: {b - a}%"></div>{/each}
      {#each marks as m, i (i)}<div class="tick k-{m.kind}" style="left: {m.pct}%"></div>{/each}
      {#each gradeMarks as m, i (i)}<div class="tick g-{m.kind}" style="left: {m.pct}%"></div>{/each}
      <input type="range" aria-label="Playhead" min={data.started} max={data.ended} step="1" value={t}
             oninput={(e) => seekTo(Number((e.currentTarget as HTMLInputElement).value))} />
    </div>
    {#if gradeMarks.length}
      <div class="gmarks">
        {#each gradeMarks as m, i (i)}
          <button class="g-{m.kind}" onclick={() => seekTo(m.t)} title={m.kind === "pass" ? "t_pass is on the scenario's clock (game time): approximate here" : ""}>
            {m.kind === "pass" ? "★" : m.kind === "gold" ? "◎" : "◆"} {clock(m.t)} {m.label}
          </button>
        {/each}
      </div>
    {/if}

    <div class="grid">
      <section class="panel screen">
        <h2>Recording <span class="right faint">{hasVideo ? (inGap ? "no video at this time" : src) : "none for this session"}</span></h2>
        <!-- svelte-ignore a11y_media_has_caption -->
        <video bind:this={video} class:hidden={!hasVideo || inGap} muted playsinline preload="metadata"
               ontimeupdate={onVideoTime} onended={onVideoEnded}></video>
        {#if !hasVideo || inGap}
          <div class="novideo faint">{hasVideo ? "Not recorded at this time: the events still play." : "No video was recorded for this session: step or play through its events."}</div>
        {/if}
      </section>

      <section class="panel events">
        <h2>At the playhead <span class="right faint">{data.events.length} events</span></h2>
        {#if shown.length}
          <ul>
            {#each shown as e, i (`${e.t}-${i}`)}
              <li class="k-{e.kind}" class:now={i === 0}>
                <span class="t faint">{clock(e.t)}</span>
                {#if e.kind === "action"}
                  <span class="tool" class:fail={!e.ok}>{e.tool.replace("mcp__bg3__", "")}</span>
                  <span class="muted args">{argLine(e.args, 70)}</span>
                  {#if i === 0}<pre>{e.result}</pre>{/if}
                {:else if e.kind === "say"}
                  <span class="say">{e.text}</span>
                {:else}
                  <span class="mem">memory: {e.subject}</span>
                {/if}
              </li>
            {/each}
          </ul>
        {:else}
          <div class="empty">Nothing yet at this time: press play or step forward.</div>
        {/if}
      </section>
    </div>
  {:else}
    <div class="empty">Loading…</div>
  {/if}
{/if}

<style>
  .bar { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; padding: 8px; }
  .bar select, .bar button {
    background: var(--panel-2); border: 1px solid var(--line); border-radius: 6px; padding: 3px 10px; cursor: pointer;
  }
  .bar .play { min-width: 44px; font-weight: 700; color: var(--accent); }
  .speeds { display: inline-flex; gap: 2px; }
  .speeds button { padding: 2px 7px; font-size: 12px; color: var(--muted); }
  .speeds button.on { color: var(--text); border-color: var(--accent); }
  .time { font: 13px var(--mono); margin-left: auto; }
  .scrub { position: relative; height: 26px; margin: 10px 0 12px; }
  .scrub input { position: absolute; inset: 0; width: 100%; margin: 0; background: transparent; accent-color: var(--accent); }
  .cov { position: absolute; top: 10px; height: 6px; background: var(--save); opacity: 0.35; border-radius: 3px; }
  .tick { position: absolute; top: 4px; width: 2px; height: 18px; opacity: 0.7; }
  .tick.k-say { background: var(--info); } .tick.k-memory { background: var(--vision); }
  .tick.g-pass { background: var(--ok); width: 3px; opacity: 1; } .tick.g-gold { background: var(--accent); }
  .tick.g-choice { background: var(--accent-2); }
  .gmarks { display: flex; flex-wrap: wrap; gap: 4px; margin: -4px 0 12px; }
  .gmarks button { background: var(--panel-2); border: 1px solid var(--line); border-radius: 6px; padding: 2px 8px;
    font-size: 12px; cursor: pointer; }
  .gmarks .g-pass { color: var(--ok); } .gmarks .g-gold { color: var(--accent); } .gmarks .g-choice { color: var(--accent-2); }
  .grid { display: grid; grid-template-columns: minmax(0, 1.4fr) minmax(0, 1fr); gap: 12px; align-items: start; }
  video { display: block; width: 100%; aspect-ratio: 16 / 9; background: #000; border-radius: 0 0 8px 8px; }
  video.hidden { display: none; }
  .novideo { aspect-ratio: 16 / 9; display: flex; align-items: center; justify-content: center; padding: 16px; text-align: center; background: var(--panel-2); border-radius: 0 0 8px 8px; }
  .events ul { list-style: none; margin: 0; padding: 0; max-height: 620px; overflow-y: auto; }
  .events li { padding: 5px 12px; border-bottom: 1px solid var(--line); font-size: 13px; display: flex; flex-wrap: wrap; gap: 2px 8px; align-items: baseline; }
  .events li.now { background: var(--panel-2); }
  .events li:not(.now) { opacity: 0.75; }
  .t { font: 11px var(--mono); }
  .tool { font: 600 12.5px var(--mono); color: var(--act); }
  .tool.fail { color: var(--bad); }
  .args { font: 12px var(--mono); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; min-width: 0; flex: 1; }
  .say { color: var(--text); white-space: pre-wrap; flex-basis: 100%; }
  .mem { color: var(--vision); }
  pre { flex-basis: 100%; margin: 4px 0 0; white-space: pre-wrap; word-break: break-word; max-height: 220px; overflow: auto; font-size: 12px; }
  @media (max-width: 900px) { .grid { grid-template-columns: 1fr; } .time { margin-left: 0; } }
</style>
