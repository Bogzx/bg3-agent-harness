<script lang="ts">
  // Every scenario of the installed suite with its start save and that save's status in the Story folder.
  import { onMount } from "svelte";
  import { api } from "../lib/api";
  import { minutes, SAVE_LABELS } from "../lib/run";
  import type { SaveStatus, ScenarioRow } from "../lib/types";

  let rows = $state<ScenarioRow[] | null>(null);
  let error = $state<string | null>(null);

  const CLASS: Record<SaveStatus, string> = {
    installed: "PASS", missing: "FAIL", wrong_hash: "FAIL", stray: "WARN", unknown: "WARN", none: "none",
  };

  onMount(() => {
    api.scenarios().then((r) => (rows = r.scenarios)).catch((e: Error) => (error = e.message));
  });

  function goal(g: Record<string, unknown> | null): string {
    return g ? Object.keys(g).join(", ") : "–";
  }
</script>

<section class="panel">
  <h2>Start saves <span class="right faint">download, verify and remove come with the saves store</span></h2>
  {#if error}
    <div class="error">{error}</div>
  {:else if !rows}
    <div class="empty">Loading…</div>
  {:else}
    <div class="tbl-wrap">
      <table>
        <thead>
          <tr><th>scenario</th><th>save</th><th>status</th><th class="num">seed</th><th>difficulty</th><th>goal</th>
            <th class="num">cap</th></tr>
        </thead>
        <tbody>
          {#each rows as s (s.name)}
            <tr>
              <td><b>{s.title ?? s.name}</b><div class="faint">{s.name}{s.holdout ? " · holdout" : ""}</div>
                {#if s.not_ready}<div class="faint">not ready: {s.not_ready}</div>{/if}</td>
              <td><code>{s.save ?? "–"}</code>{#if s.save_sha1}<div class="faint"><code>sha1 {s.save_sha1.slice(0, 12)}</code></div>{/if}</td>
              <td><span class="st {CLASS[s.save_status.status]}" title={s.save_status.detail}>{SAVE_LABELS[s.save_status.status]}</span></td>
              <td class="num">{s.seed ?? "–"}</td>
              <td>{s.difficulty ?? "–"}</td>
              <td>{goal(s.goal)}</td>
              <td class="num">{minutes(s.cap_s)}</td>
            </tr>
          {/each}
        </tbody>
      </table>
    </div>
  {/if}
</section>
