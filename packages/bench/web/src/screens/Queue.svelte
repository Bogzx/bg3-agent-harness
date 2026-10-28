<script lang="ts">
  // The matrices on this machine and their progress (runtime/matrix/*/progress.json), refreshed every 5 s, and the
  // one playing now with Stop: the one this panel started, or one it found running (another panel, a terminal, this
  // panel before a restart), which Stop ends through runtime/STOP.
  import { onMount } from "svelte";
  import { api } from "../lib/api";
  import { date, usd } from "../lib/format";
  import { autoSummary, choicesLine } from "../lib/run";
  import type { FoundMatrix, MatrixProgress, RunState } from "../lib/types";

  let rows = $state<MatrixProgress[] | null>(null);
  let error = $state<string | null>(null);
  let mine = $state<RunState | null>(null);
  let found = $state<FoundMatrix | null>(null);
  let stopError = $state<string | null>(null);

  function load() {
    Promise.all([api.queue(), api.run()]).then(([q, r]) => {
      rows = q.matrices;
      mine = r.run;
      found = r.found;
      error = null;
    }).catch((e: Error) => (error = e.message));
  }

  async function stop() {
    if (!confirm("Stop: the run playing now ends (recorded as an operator stop) and no new run starts. Stop?")) return;
    stopError = null;
    try {
      const r = await api.stop();
      mine = r.run;
      found = r.found;
    } catch (e) {
      stopError = (e as Error).message;
    }
  }

  onMount(() => {
    load();
    const t = setInterval(load, 5000);
    return () => clearInterval(t);
  });
</script>

{#if mine}
  <section class="panel" aria-label="This panel's run">
    <h2>Started here: {mine.name}</h2>
    <div class="body row">
      <span>{mine.running ? (mine.stopping ? "stopping: the run playing now ends and is recorded" : "running") :
        `ended (exit ${mine.returncode ?? "?"})`}</span>
      {#if mine.log}<span class="faint">log {mine.log}</span>{/if}
      {#if mine.running && !mine.stopping}<button class="plain" onclick={stop}>Stop</button>{/if}
    </div>
    {#if choicesLine(mine.choices)}<div class="body faint">{choicesLine(mine.choices)}</div>{/if}
    {#if autoSummary(mine.choices)}<div class="body">Submit when done: {autoSummary(mine.choices)}</div>{/if}
    {#if stopError && !found}<div class="note bad">{stopError}</div>{/if}
  </section>
{/if}

{#if found}
  <section class="panel" aria-label="Running matrix">
    <h2>Running: {found.name}</h2>
    <div class="body row">
      <span>{found.stopping ? "stopping: the run playing now ends and is recorded" : "running"} (pid {found.pid},
        not started by this panel)</span>
      <span class="faint">{found.matrix_file}</span>
      {#if !found.stopping}<button class="plain" onclick={stop}>Stop</button>{/if}
    </div>
    {#if choicesLine(found.choices)}<div class="body faint">{choicesLine(found.choices)}</div>{/if}
    {#if autoSummary(found.choices)}<div class="body">Submit when done: {autoSummary(found.choices)}</div>{/if}
    {#if stopError}<div class="note bad">{stopError}</div>{/if}
  </section>
{/if}

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
