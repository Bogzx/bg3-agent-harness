<script lang="ts">
  // A table's horizontal scroll box: a named region keyboard users can focus and scroll (narrow screens).
  import type { Snippet } from "svelte";

  let { label, children }: { label: string; children: Snippet } = $props();
</script>

<!-- svelte-ignore a11y_no_noninteractive_tabindex -->
<div class="tbl-wrap" role="region" aria-label={label} tabindex="0">{@render children()}</div>
