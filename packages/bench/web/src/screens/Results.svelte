<script lang="ts">
  // Finished runs on this machine (result.json + manifest.json): verdict or score, time, cost, validity, the
  // attempt number within the run's cell, and the size of its kept saves.
  import { onMount } from "svelte";
  import { api } from "../lib/api";
  import { usd } from "../lib/format";
  import { attempts, bytes, minutes, verdict } from "../lib/run";
  import type { RunRow } from "../lib/types";

  let rows = $state<RunRow[] | null>(null);
  let error = $state<string | null>(null);
  const n = $derived(attempts(rows ?? []));

  onMount(() => {
    api.results().then((r) => (rows = r.runs)).catch((e: Error) => (error = e.message));
  });
</script>

<section class="panel">
  <h2>Results <span class="right faint">render, validate and submit come with the bundle and video tools</span></h2>
  {#if error}
    <div class="error">{error}</div>
  {:else if !rows}
    <div class="empty">Loading…</div>
  {:else if !rows.length}
    <div class="empty">No finished run yet.</div>
  {:else}
    <div class="tbl-wrap">
      <table>
        <thead><tr><th>run</th><th>scenario</th><th>model</th><th>verdict</th><th class="num">wall</th>
          <th class="num">cost</th><th class="num">luck z</th><th class="num">attempt</th><th class="num">saves</th></tr></thead>
        <tbody>
          {#each rows as r (r.dir)}
            {@const a = n.get(r.dir)}
            <tr>
              <td><code>{r.run_id ?? r.dir}</code>{#if r.pilot.length}<div class="faint">pilot</div>{/if}</td>
              <td>{r.scenario ?? "–"}</td>
              <td>{r.model ?? "–"}<div class="faint">{r.client ?? ""} · {r.condition ?? ""}</div></td>
              <td><span class="st {r.valid === false ? 'FAIL' : r.passed === true ? 'PASS' : 'none'}">{verdict(r)}</span>
                {#if r.t_pass_s !== null}<div class="faint">at {minutes(r.t_pass_s)}</div>{/if}</td>
              <td class="num">{minutes(r.wall_s)}</td>
              <td class="num">{usd(r.cost_usd)}</td>
              <td class="num">{r.luck_z === null ? "–" : r.luck_z.toFixed(2)}</td>
              <td class="num">{a ? `${a[0]} of ${a[1]}` : "–"}</td>
              <td class="num">{r.saves_bytes ? bytes(r.saves_bytes) : "–"}</td>
            </tr>
          {/each}
        </tbody>
      </table>
    </div>
  {/if}
</section>
