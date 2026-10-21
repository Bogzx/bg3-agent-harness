<script lang="ts">
  import { onMount } from "svelte";
  import { api } from "../lib/api";
  import { date, num } from "../lib/format";
  import { buildRows, fightLabel, hardFight, type Filter } from "../lib/timeline";
  import type { TimelinePayload } from "../lib/types";

  let { pt }: { pt: string } = $props();

  let data = $state.raw<TimelinePayload | null>(null);
  let error = $state<string | null>(null);
  let filter = $state<Filter>({ choices: true, fights: true, hardOnly: false });
  let open = $state<string | null>(null);

  onMount(() => {
    api
      .timeline(pt)
      .then((t) => (data = t))
      .catch((e: Error) => (error = e.message));
  });

  const rows = $derived(data ? buildRows(data, filter) : []);
  const toggle = (key: string) => (open = open === key ? null : key);
</script>

{#if error}
  <div class="error">{error}</div>
{:else if !data}
  <div class="empty">Loading…</div>
{:else}
  <div class="filters panel">
    <label><input type="checkbox" bind:checked={filter.choices} /> <span class="ic choice">◆</span> choices</label>
    <label><input type="checkbox" bind:checked={filter.fights} /> <span class="ic fight">●</span> fights</label>
    <label><input type="checkbox" bind:checked={filter.hardOnly} /> hard fights only</label>
    <span class="faint count">{data.decisions.length} choices · {data.fights.length} fights</span>
  </div>

  {#each rows as r (r.session)}
    <section class="session">
      <div class="head">
        <h3>{r.session}</h3>
        <span class="faint">{date(r.started)}{r.minutes ? ` · ${num(r.minutes, 0)} min` : ""}</span>
        {#if r.outcome && r.outcome !== "success"}<span class="chip bad">{r.outcome}</span>{/if}
        <div class="track" aria-hidden="true">
          {#each r.items as it, i (i)}
            {#if it.kind === "choice"}
              <span class="ic choice" title={it.d.title}>◆</span>
            {:else}
              <span class="ic fight" class:hard={hardFight(it.f)} class:death={it.f.died.length > 0} title={fightLabel(it.f)}>
                {it.f.died.length ? "✖" : "●"}
              </span>
            {/if}
          {/each}
        </div>
      </div>
      <ul class="items">
        {#each r.items as it, i (i)}
          {@const key = `${r.session}-${i}`}
          <li class="panel">
            <button class="row" onclick={() => toggle(key)} aria-expanded={open === key}>
              {#if it.kind === "choice"}
                <span class="ic choice">◆</span>
                <span class="title"><span class="faint">{it.d.id}</span> {it.d.title}</span>
              {:else}
                <span class="ic fight" class:hard={hardFight(it.f)} class:death={it.f.died.length > 0}>{it.f.died.length ? "✖" : "●"}</span>
                <span class="title">{fightLabel(it.f)}</span>
                <span class="faint meta">{it.f.region ?? ""} · {it.f.rounds ?? "?"} rounds · {it.f.grade ?? ""}</span>
              {/if}
            </button>
            {#if open === key}
              <div class="card">
                {#if it.kind === "choice"}
                  {#each it.d.choice as c, j (j)}<p><b>Choice</b> {c}</p>{/each}
                  {#each it.d.why as w, j (j)}<p><b>Why</b> {w}</p>{/each}
                  {#each it.d.consequences as c, j (j)}<p><b>Consequence</b> {c.text} {#if c.tag}<span class="faint">[{c.tag}]</span>{/if}</p>{/each}
                  {#each it.d.expected as c, j (j)}<p><b>Expected</b> {c.text} {#if c.tag}<span class="faint">[{c.tag}]</span>{/if}</p>{/each}
                  {#if it.d.reversible}<p><b>Reversible</b> {it.d.reversible}</p>{/if}
                {:else}
                  <p><b>Enemies</b> {it.f.enemies.join(", ") || "–"}</p>
                  <p><b>Party</b> {it.f.downed} downed{it.f.died.length ? ` · died: ${it.f.died.join(", ")}` : ""}{it.f.reloaded ? " · reloaded" : ""}</p>
                  <p class="faint">{it.f.t ?? ""} · {num(it.f.duration_s, 0, " s")}</p>
                {/if}
              </div>
            {/if}
          </li>
        {/each}
      </ul>
    </section>
  {:else}
    <div class="empty">Nothing to show with these filters.</div>
  {/each}
{/if}

<style>
  .filters { display: flex; flex-wrap: wrap; gap: 6px 16px; align-items: center; padding: 8px 12px; margin-bottom: 12px; }
  .filters label { display: inline-flex; gap: 6px; align-items: center; cursor: pointer; }
  .count { margin-left: auto; font-size: 12px; }
  .session { margin-bottom: 18px; }
  .head { display: flex; flex-wrap: wrap; align-items: baseline; gap: 4px 10px; margin: 0 2px 6px; }
  .head h3 { margin: 0; font: 600 15px var(--mono); }
  .chip.bad { color: var(--bad); }
  .track { display: flex; flex-wrap: wrap; gap: 2px; flex: 1 1 100%; min-width: 0; }
  .ic { font-size: 12px; line-height: 1; }
  .ic.choice { color: var(--accent-2); }
  .ic.fight { color: var(--ok); }
  .ic.fight.hard { color: var(--warn); }
  .ic.fight.death { color: var(--bad); }
  .items { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 4px; }
  .row {
    width: 100%; display: flex; gap: 8px; align-items: baseline; text-align: left; background: none; border: 0;
    padding: 7px 12px; cursor: pointer; flex-wrap: wrap;
  }
  .title { font-weight: 550; flex: 1 1 200px; min-width: 0; }
  .meta { font-size: 12px; }
  .card { padding: 0 12px 10px 32px; font-size: 13.5px; }
  .card p { margin: 4px 0; }
  .card b { color: var(--muted); font-weight: 600; margin-right: 4px; }
</style>
