<script lang="ts">
  // Can this machine run the harness at all: bg3harness.doctor's checks, each failure with what to do about it.
  import { onMount } from "svelte";
  import { api } from "../lib/api";
  import type { DoctorReport } from "../lib/types";

  let report = $state<DoctorReport | null>(null);
  let error = $state<string | null>(null);
  let busy = $state(false);

  async function load() {
    busy = true;
    error = null;
    try {
      const r = await api.doctor();
      report = r.report;
      error = r.error;
    } catch (e) {
      error = (e as Error).message;
    } finally {
      busy = false;
    }
  }

  onMount(load);
</script>

<section class="panel">
  <h2>
    Doctor
    <span class="right">
      {#if report}<span class="st {report.ready ? 'PASS' : 'FAIL'}">{report.ready ? "READY" : "NOT READY"}</span>
        {report.fails} fail · {report.warns} warn{/if}
      <button class="plain" onclick={load} disabled={busy}>{busy ? "Checking…" : "Check again"}</button>
    </span>
  </h2>
  {#if error}
    <div class="error">{error}</div>
  {:else if !report}
    <div class="empty">Checking…</div>
  {:else}
    <div class="tbl-wrap">
      <table>
        <tbody>
          {#each report.checks as c, i (i)}
            <tr>
              <td><span class="st {c.status}">{c.status}</span></td>
              <td><b>{c.name}</b></td>
              <td>{c.detail}{#if c.fix}<div class="fix">Fix: {c.fix}</div>{/if}</td>
            </tr>
          {/each}
        </tbody>
      </table>
    </div>
  {/if}
</section>

<style>
  .fix { margin-top: 4px; color: var(--muted); }
</style>
