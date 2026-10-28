<script lang="ts">
  import { onMount } from "svelte";
  import { api } from "../lib/api";
  import type { About } from "../lib/types";

  let data = $state<About | null>(null);
  let error = $state<string | null>(null);

  onMount(() => {
    api.about().then((r) => (data = r)).catch((e: Error) => (error = e.message));
  });
</script>

<section class="panel">
  <h2>About</h2>
  <div class="body">
    {#if error}
      <div class="error">{error}</div>
    {:else if data}
      <p>bg3bench {data.bench_version} · suite <b>{data.suite}</b></p>
      {#if data.site}
        <p><a href={data.site} target="_blank" rel="noopener noreferrer">{data.site}</a>: the leaderboard, terms and privacy.</p>
      {/if}
      <p class="muted">{data.not_affiliated}</p>
    {/if}
  </div>
</section>
