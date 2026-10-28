<script lang="ts">
  // The matrices on this machine and their progress (runtime/matrix/*/progress.json), refreshed every 5 s.
  import { onMount } from "svelte";
  import { api } from "../lib/api";
  import { date, usd } from "../lib/format";
  import type { MatrixProgress } from "../lib/types";

  let rows = $state<MatrixProgress[] | null>(null);
  let error = $state<string | null>(null);

  function load() {
    api.queue().then((r) => {
      rows = r.matrices;
      error = null;
    }).catch((e: Error) => (error = e.message));
  }

  onMount(() => {
    load();
    const t = setInterval(load, 5000);
    return () => clearInterval(t);
  });
</script>

<section class="panel">
  <h2>Queue</h2>
  {#if error}
    <div class="error">{error}</div>
  {:else if !rows}
    <div class="empty">Loading…</div>
  {:else if !rows.length}
    <div class="empty">No matrix has run on this machine yet.</div>
  {:else}
    <div class="tbl-wrap">
      <table>
        <thead><tr><th>matrix</th><th>state</th><th>current</th><th class="num">done / jobs</th><th class="num">runs</th>
          <th class="num">invalid</th><th class="num">spent / cap</th><th>updated</th></tr></thead>
        <tbody>
          {#each rows as m (m.matrix)}
            <tr>
              <td><b>{m.matrix}</b></td>
              <td>{m.state ?? "?"}{m.stop_reason ? ` (${m.stop_reason})` : ""}</td>
              <td>{m.current?.id ?? "–"}{m.current?.attempt ? ` · attempt ${m.current.attempt}` : ""}</td>
              <td class="num">{m.done ?? 0} / {m.jobs ?? 0}</td>
              <td class="num">{m.runs ?? 0}</td>
              <td class="num">{m.invalid_runs ?? 0}</td>
              <td class="num">{usd(m.spent_usd)} / {usd(m.cap_usd)}</td>
              <td>{date(m.updated)}</td>
            </tr>
          {/each}
        </tbody>
      </table>
    </div>
  {/if}
</section>
