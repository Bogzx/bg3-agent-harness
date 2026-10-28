<script lang="ts">
  // Can this machine run the benchmark: bg3bench.preflight's checks and the panel's (ffmpeg, display, disk).
  import { onMount } from "svelte";
  import { api } from "../lib/api";
  import type { Preflight } from "../lib/types";

  let data = $state<Preflight | null>(null);
  let error = $state<string | null>(null);
  let busy = $state(false);

  async function load() {
    busy = true;
    error = null;
    try {
      data = await api.preflight();
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
    Preflight
    <span class="right">
      {#if data}<span class="st {data.ready ? 'PASS' : 'FAIL'}">{data.ready ? "READY" : "NOT READY"}</span>
        {data.fails} fail · {data.warns} warn{/if}
      <button class="plain" onclick={load} disabled={busy}>{busy ? "Checking…" : "Check again"}</button>
    </span>
  </h2>
  {#if error}
    <div class="error">{error}</div>
  {:else if !data}
    <div class="empty">Checking…</div>
  {:else}
    <div class="tbl-wrap">
      <table>
        <tbody>
          {#each data.checks as c, i (i)}
            <tr><td><span class="st {c.status}">{c.status}</span></td><td><b>{c.name}</b></td><td>{c.detail}</td></tr>
          {/each}
        </tbody>
      </table>
    </div>
  {/if}
</section>
