<script lang="ts">
  // Every scenario of the installed suite with its start save and that save's status in the Story folder.
  import { onMount } from "svelte";
  import TableWrap from "../lib/TableWrap.svelte";
  import { api } from "../lib/api";
  import { minutes, saveChip } from "../lib/run";
  import type { ScenarioRow } from "../lib/types";

  let rows = $state<ScenarioRow[] | null>(null);
  let error = $state<string | null>(null);

  onMount(() => {
    api.scenarios().then((r) => (rows = r.scenarios)).catch((e: Error) => (error = e.message));
  });

  function goal(g: Record<string, unknown> | null): string {
    return g ? Object.keys(g).join(", ") : "–";
  }
</script>

<section class="panel">
  <h2>Start saves <span class="right faint">status only: the panel can't download or remove a save yet</span></h2>
  {#if error}
    <div class="error">{error}</div>
  {:else if !rows}
    <div class="empty">Loading…</div>
  {:else}
    <TableWrap label="Scenarios and saves">
      <table>
        <thead>
          <tr><th>scenario</th><th>save</th><th>status</th><th class="num">seed</th><th>difficulty</th><th>goal</th>
            <th class="num">cap</th></tr>
        </thead>
        <tbody>
          {#each rows as s (s.name)}
            {@const chip = saveChip(s)}
            <tr>
              <td><b>{s.title ?? s.name}</b><div class="faint">{s.name}{s.holdout ? " · holdout" : ""}</div>
                {#if s.not_ready}<details class="faint"><summary>Not available yet</summary>{s.not_ready}</details>{/if}</td>
              <td><code>{s.save ?? "–"}</code>{#if s.save_sha1}<div class="faint"><code>sha1 {s.save_sha1.slice(0, 12)}</code></div>{/if}</td>
              <td><span class="st {chip.cls}" title={chip.title}>{chip.label}</span></td>
              <td class="num">{s.seed ?? "–"}</td>
              <td>{s.difficulty ?? "–"}</td>
              <td>{goal(s.goal)}</td>
              <td class="num nowrap">{minutes(s.cap_s)}</td>
            </tr>
          {/each}
        </tbody>
      </table>
    </TableWrap>
  {/if}
</section>
