<script lang="ts">
  // /overlay: a transparent 1920x1080 page for OBS (a browser source). No scrolling, big type.
  import { onMount } from "svelte";
  import { api, openStream } from "./lib/api";
  import { hpPct, mmss, usd } from "./lib/format";
  import { clip, parseOverlay } from "./lib/overlay";
  import { applyEvent, fromSnapshot, says, secondsLeft, type WatchState } from "./lib/watch";

  const opt = parseOverlay(location.search);
  const on = (p: Parameters<typeof opt.panels.has>[0]) => opt.panels.has(p);
  let s = $state.raw<WatchState | null>(null);
  let error = $state<string | null>(null);
  let now = $state(Date.now() / 1000);
  let close: (() => void) | null = null;

  async function load() {
    close?.();
    try {
      let pt = opt.pt;
      if (!pt) {
        const list = (await api.playthroughs()).playthroughs;
        pt = (list.find((p) => p.current) ?? list[list.length - 1])?.name ?? null;
      }
      if (!pt) throw new Error("no playthrough");
      const snap = await api.watch(pt);
      s = fromSnapshot(snap);
      error = null;
      close = openStream(snap.stream_id, (type, data) => {
        if (s) s = applyEvent(s, type, data);
      }, () => void load(), () => {});
    } catch (e) {
      error = (e as Error).message;
    }
  }

  onMount(() => {
    document.documentElement.classList.add("overlay");
    document.documentElement.dataset.theme = opt.theme;
    void load();
    const tick = setInterval(() => (now = Date.now() / 1000), 1000);
    return () => {
      clearInterval(tick);
      close?.();
    };
  });

  const line = $derived(s ? says(s, 1)[0] : undefined);
  const call = $derived(s ? s.actions[s.actions.length - 1] : undefined);
  const left = $derived(s ? secondsLeft(s.runner, now) : null);
</script>

<div class="stage">
  {#if error}
    <div class="box err">overlay: {error}</div>
  {:else if s}
    {#if on("tool") && call}
      <div class="box tool">
        <div class="label">last action</div>
        <div class="name" class:fail={!call.ok}>{call.tool.replace("mcp__bg3__", "")} <span class="ms">{call.ms ?? "–"} ms</span></div>
        <div class="res">{clip(call.result, 150)}</div>
      </div>
    {/if}
    <div class="bottom">
      {#if on("goal")}
        <div class="box goal"><div class="label">goal</div><div class="text">{clip(s.goal ?? "–", 110)}</div></div>
      {/if}
      {#if on("say")}
        <div class="box say"><div class="label">agent</div><div class="text">{line ? clip(line.text, 190) : "…"}</div></div>
      {/if}
      {#if on("party") && s.party?.party.length}
        <div class="box party">
          {#each s.party.party.slice(0, 4) as m (m.name)}
            <div class="m" class:down={m.dead || m.downed}>
              <span class="n">{m.active ? "★ " : ""}{clip(m.name, 14)}</span>
              <span class="hp">{m.hp}/{m.max_hp}</span>
              <div class="bar"><div style="width: {hpPct(m.hp, m.max_hp)}%" class:low={hpPct(m.hp, m.max_hp) < 35}></div></div>
            </div>
          {/each}
        </div>
      {/if}
      {#if on("clock")}
        <div class="box clock">
          <div class="label">{s.session ?? "session"}</div>
          <div class="big">{left === null ? "ended" : mmss(left)}</div>
        </div>
      {/if}
      {#if on("cost")}
        <div class="box cost">
          <div class="label">cost</div>
          <div class="big">{usd(s.cost.total_usd)}</div>
          <div class="last">last {usd(s.cost.last_session_usd)}</div>
        </div>
      {/if}
    </div>
  {/if}
</div>

<style>
  :global(html.overlay), :global(html.overlay body) { background: transparent !important; overflow: hidden; }
  .stage { position: relative; width: 1920px; height: 1080px; overflow: hidden; font-size: 24px; color: var(--text); }
  .box {
    background: color-mix(in srgb, var(--panel) 82%, transparent); border: 1px solid var(--line); border-radius: 12px;
    padding: 12px 18px; box-shadow: 0 6px 24px rgba(0, 0, 0, 0.35);
  }
  .label { font-size: 15px; font-weight: 700; letter-spacing: 0.1em; text-transform: uppercase; color: var(--accent); margin-bottom: 4px; }
  .tool { position: absolute; top: 32px; right: 32px; width: 520px; }
  .tool .name { font: 600 26px var(--mono); }
  .tool .name.fail { color: var(--bad); }
  .tool .ms { font-size: 18px; color: var(--muted); }
  .tool .res { font-size: 18px; color: var(--muted); margin-top: 4px; line-height: 1.3; }
  .bottom { position: absolute; left: 32px; right: 32px; bottom: 32px; display: flex; gap: 16px; align-items: stretch; height: 150px; }
  .goal { flex: 0 0 380px; }
  .say { flex: 1; min-width: 0; }
  .text { line-height: 1.3; overflow: hidden; display: -webkit-box; -webkit-line-clamp: 3; line-clamp: 3; -webkit-box-orient: vertical; }
  .say .text { font-size: 26px; }
  .party { flex: 0 0 380px; display: flex; flex-direction: column; justify-content: center; gap: 6px; padding: 10px 16px; }
  .m { display: grid; grid-template-columns: 1fr auto; column-gap: 10px; font-size: 18px; }
  .m .hp { font: 16px var(--mono); }
  .m.down .n { color: var(--bad); }
  .bar { grid-column: 1 / -1; height: 6px; background: var(--hp-bg); border-radius: 3px; overflow: hidden; }
  .bar div { height: 100%; background: var(--ok); }
  .bar div.low { background: var(--bad); }
  .clock, .cost { text-align: center; display: flex; flex-direction: column; justify-content: center; }
  .clock { flex: 0 0 170px; }
  .cost { flex: 0 0 200px; }
  .big { font: 700 44px var(--mono); }
  .last { font-size: 16px; color: var(--muted); margin-top: 2px; }
  .err { position: absolute; bottom: 32px; left: 32px; color: var(--bad); }
</style>
