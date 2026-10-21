<script lang="ts">
  import { project } from "../lib/breadcrumb";
  import type { Positions } from "../lib/types";

  let { pos, onLevel, w = 316, h = 200 }: { pos: Positions; onLevel: (level: string) => void; w?: number; h?: number } =
    $props();
  const plot = $derived(project(pos, w, h));
  const region = $derived(pos.levels.find((l) => l.level === pos.level)?.region ?? pos.level);
</script>

<section class="panel o6">
  <h2>
    Map
    <span class="right">
      {#if pos.levels.length > 1}
        <select aria-label="Level" value={pos.level ?? ""} onchange={(e) => onLevel((e.currentTarget as HTMLSelectElement).value)}>
          {#each pos.levels as l (l.level)}<option value={l.level}>{l.region ?? l.level}</option>{/each}
        </select>
      {:else}<span class="faint">{region ?? ""}</span>{/if}
    </span>
  </h2>
  {#if plot.dots.length}
    <svg viewBox="0 0 {w} {h}" role="img" aria-label="Party breadcrumb trail, north up">
      {#each plot.lines as pts, i (i)}
        <polyline points={pts} class="trail" />
      {/each}
      {#if plot.start}<circle cx={plot.start.cx} cy={plot.start.cy} r="3" class="start" />{/if}
      {#each plot.dots as d (d.name)}
        <circle cx={d.cx} cy={d.cy} r={d.active ? 5 : 3.5} class:active={d.active} class="member"><title>{d.name}</title></circle>
      {/each}
      <g class="scale" transform="translate({w - 62},{h - 10})">
        <line x1="0" y1="0" x2="50" y2="0" /><text x="25" y="-4">{plot.scale_m} m</text>
      </g>
      <text x="8" y="14" class="north">N ↑</text>
    </svg>
  {:else}
    <div class="empty">No positions yet.</div>
  {/if}
</section>

<style>
  select { background: var(--panel); border: 1px solid var(--line); border-radius: 4px; font-size: 11px; padding: 0 4px; }
  svg { display: block; width: 100%; height: auto; background: var(--panel-2); border-radius: 0 0 8px 8px; }
  .trail { fill: none; stroke: var(--accent); stroke-width: 1.6; stroke-linejoin: round; opacity: 0.85; }
  .start { fill: var(--muted); }
  .member { fill: var(--info); stroke: var(--panel); stroke-width: 1.5; }
  .member.active { fill: var(--accent); }
  .scale line { stroke: var(--muted); stroke-width: 1.5; }
  .scale text, .north { fill: var(--muted); font: 10px var(--mono); text-anchor: middle; }
  .north { text-anchor: start; }
</style>
