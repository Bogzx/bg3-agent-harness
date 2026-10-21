<script lang="ts">
  // The story so far from the memory: chapter summaries, PLAYTHROUGH.md and the party snapshot (decisions are on
  // the Timeline screen).
  import { onMount } from "svelte";
  import { api } from "../lib/api";
  import { href } from "../lib/route";
  import { hpPct } from "../lib/format";
  import type { StoryItem, StoryPayload } from "../lib/types";

  let { pt }: { pt: string } = $props();

  type Tab = "chapters" | "overview" | "party";
  let tab = $state<Tab>("chapters");
  let d = $state.raw<StoryPayload | null>(null);
  let error = $state<string | null>(null);

  onMount(() => {
    api
      .story(pt)
      .then((s) => (d = s))
      .catch((e: Error) => (error = e.message));
  });

  const members = $derived(d ? [...d.status.party, ...d.others.map((o) => ({ ...o, camp: true }))] : []);
  const itemClass = (i: StoryItem) => (i.story ? "r-story" : i.rarity ? `r-${i.rarity.split(" ")[0]}` : "");
</script>

{#if error}
  <div class="error">{error}</div>
{:else if !d}
  <div class="empty">Loading…</div>
{:else if !d.exists}
  <div class="empty">No memory directory at {d.dir}</div>
{:else}
  <div class="tabs panel">
    {#each [["chapters", "Chapters"], ["overview", "Overview"], ["party", "Party"]] as [k, label] (k)}
      <button class:on={tab === k} onclick={() => (tab = k as Tab)}>{label}</button>
    {/each}
    <span class="faint count">{d.chapters.length} chapters ·
      <a href={href({ screen: "timeline", pt, file: null })}>{d.decisions.length} decisions on the timeline</a></span>
  </div>

  {#if tab === "chapters"}
    {#each d.chapters.slice().reverse() as c (c.file)}
      <section class="panel"><h2>{c.title}</h2><div class="body">
        {#each c.sections as s, i (i)}
          {#if s.heading}<h4>{s.heading}</h4>{/if}
          <ul>{#each s.items as it, j (j)}<li>{it}</li>{/each}</ul>
        {/each}
      </div></section>
    {:else}
      <div class="empty">No chapter summaries yet (memory/chapters/).</div>
    {/each}
  {:else if tab === "overview"}
    {@const o = d.overview}
    <section class="panel"><div class="body">
      {#if o.title}<h3>{o.title}</h3>{/if}
      {#if o.now}<p class="now">{o.now}</p>{/if}
      {#each o.header as h, i (i)}<div class="muted">{h}</div>{/each}
      {#if o.main_thread}<h4>Main thread</h4><p>{o.main_thread}</p>{/if}
      {#if o.party.length}<h4>Party &amp; companions</h4><ul>{#each o.party as p, i (i)}<li>{p}</li>{/each}</ul>{/if}
      {#if o.commitments.length}<h4>Standing commitments</h4><ul>{#each o.commitments as c, i (i)}<li>{c}</li>{/each}</ul>{/if}
      {#if !o.title && !o.now && !o.header.length}<div class="empty">No PLAYTHROUGH.md yet.</div>{/if}
    </div></section>
  {:else}
    <div class="where muted">{d.status.where ?? ""}{d.status.mode ? ` · ${d.status.mode}` : ""}{d.status.gold != null ? ` · ${d.status.gold} gold` : ""}
      {#if d.status.when}<span class="faint">snapshot {d.status.when}</span>{/if}</div>
    <div class="cards">
      {#each members as m (m.name)}
        <section class="panel"><div class="body">
          <b>{m.active ? "★ " : ""}{m.name}</b> <span class="faint">{m.class ?? (m.camp ? "not in the party snapshot" : "")}</span>
          {#if m.max_hp}
            <div class="bar"><div class:low={hpPct(m.hp ?? 0, m.max_hp) < 35} style="width: {hpPct(m.hp ?? 0, m.max_hp)}%"></div></div>
            <div class="small">HP {m.hp}/{m.max_hp}{m.statuses?.length ? ` · ${m.statuses.join(", ")}` : ""}</div>
          {/if}
          {#if m.equipped?.length}<div class="small">Equipped: {#each m.equipped as it, i (i)}{i ? ", " : ""}<span class={itemClass(it)}>{it.name}</span>{it.slot ? ` (${it.slot})` : ""}{/each}</div>{/if}
          {#if m.notable?.length}<div class="small">Bags: {#each m.notable as it, i (i)}{i ? ", " : ""}<span class={itemClass(it)}>{it.name}</span>{/each}</div>{/if}
        </div></section>
      {:else}
        <div class="empty">No party snapshot yet (memory/snapshots/).</div>
      {/each}
    </div>
  {/if}
{/if}

<style>
  .tabs { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; padding: 8px; margin-bottom: 12px; }
  .tabs button { background: none; border: 1px solid var(--line); border-radius: 6px; padding: 3px 10px; cursor: pointer; color: var(--muted); }
  .tabs button.on { color: var(--text); border-color: var(--accent); }
  .count { margin-left: auto; font-size: 12.5px; }
  section.panel { margin-bottom: 12px; }
  h2 { text-transform: none !important; letter-spacing: 0 !important; font-size: 14px !important; color: var(--text) !important; }
  h4 { margin: 10px 0 4px; font-size: 13px; color: var(--muted); }
  ul { margin: 4px 0; padding-left: 1.3em; }
  li { margin: 2px 0; }
  .now { font-size: 15px; font-weight: 550; }
  .where { margin-bottom: 8px; display: flex; gap: 8px; flex-wrap: wrap; }
  .cards { display: grid; grid-template-columns: repeat(auto-fill, minmax(260px, 1fr)); gap: 12px; }
  .cards section.panel { margin: 0; }
  .small { font-size: 12.5px; margin-top: 3px; }
  .bar { height: 6px; background: var(--hp-bg); border-radius: 3px; overflow: hidden; margin-top: 6px; }
  .bar div { height: 100%; background: var(--ok); }
  .bar div.low { background: var(--bad); }
  .r-story { color: var(--accent); }
  .r-uncommon { color: var(--ok); }
  .r-rare { color: var(--info); }
  .r-very { color: var(--vision); }
  .r-legendary { color: var(--warn); }
</style>
