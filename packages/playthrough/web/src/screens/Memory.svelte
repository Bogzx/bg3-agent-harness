<script lang="ts">
  import { onMount } from "svelte";
  import { api } from "../lib/api";
  import { date } from "../lib/format";
  import { groupFiles, renderMarkdown, type Folder } from "../lib/memory";
  import type { Commit } from "../lib/types";

  let { pt, file, onOpen }: { pt: string; file: string | null; onOpen: (path: string) => void } = $props();

  let folders = $state<Folder[]>([]);
  let commits = $state<Commit[]>([]);
  let error = $state<string | null>(null);
  let doc = $state<{ path: string; text: string; history: Commit[] } | null>(null);
  let docError = $state<string | null>(null);
  let raw = $state(false);

  onMount(() => {
    api
      .memoryTree(pt)
      .then((t) => (folders = groupFiles(t.files)))
      .catch((e: Error) => (error = e.message));
    api
      .memoryLog(pt)
      .then((l) => (commits = l.commits))
      .catch(() => (commits = []));
  });

  const current = $derived(file ?? folders[0]?.files[0]?.path ?? null);

  $effect(() => {
    const path = current;
    if (!path) return;
    docError = null;
    api
      .memoryFile(pt, path)
      .then((d) => {
        if (path === current) doc = d;
      })
      .catch((e: Error) => (docError = e.message));
  });

  const html = $derived(doc && !raw && doc.path.endsWith(".md") ? renderMarkdown(doc.text) : null);
</script>

{#if error}
  <div class="error">{error}</div>
{:else}
  <div class="layout">
    <nav class="panel files" aria-label="Memory files">
      <h2>Files</h2>
      {#each folders as f (f.name)}
        {#if f.name}<div class="folder faint">{f.name}/</div>{/if}
        <ul>
          {#each f.files as m (m.path)}
            <li>
              <button class:active={m.path === current} onclick={() => onOpen(m.path)}>
                {f.name ? m.path.slice(f.name.length + 1) : m.path}
              </button>
            </li>
          {/each}
        </ul>
      {/each}
    </nav>

    <article class="panel doc">
      <h2>
        {current ?? "–"}
        <span class="right">
          <button class="toggle" onclick={() => (raw = !raw)}>{raw ? "rendered" : "raw"}</button>
        </span>
      </h2>
      {#if docError}
        <div class="error">{docError}</div>
      {:else if !doc}
        <div class="empty">Loading…</div>
      {:else if html !== null}
        <div class="body md">{@html html}</div>
      {:else}
        <pre class="body rawtext">{doc.text}</pre>
      {/if}
      {#if doc?.history.length}
        <div class="hist faint">
          Edited in:
          {#each doc.history as c (c.sha)}
            <span class="chip" title="{c.subject} · {date(c.t)}">{c.session ?? c.sha.slice(0, 7)}</span>
          {/each}
        </div>
      {/if}
    </article>

    <aside class="panel commits">
      <h2>Memory commits</h2>
      {#if commits.length}
        <ul>
          {#each commits as c (c.sha)}
            <li><span class="faint">{date(c.t)}</span><br />{c.subject}</li>
          {/each}
        </ul>
      {:else}
        <div class="empty">No git history for this memory.</div>
      {/if}
    </aside>
  </div>
{/if}

<style>
  .layout { display: grid; grid-template-columns: 220px minmax(0, 1fr) 260px; gap: 12px; align-items: start; }
  .files ul, .commits ul { list-style: none; margin: 0; padding: 4px 6px 8px; }
  .folder { padding: 8px 12px 0; font: 11px var(--mono); }
  .files button {
    width: 100%; text-align: left; background: none; border: 0; padding: 3px 6px; border-radius: 4px; cursor: pointer;
    color: var(--muted); font-size: 13px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  }
  .files button:hover { background: var(--panel-2); color: var(--text); }
  .files button.active { background: var(--panel-2); color: var(--text); box-shadow: inset 2px 0 0 var(--accent); }
  .doc > h2 { text-transform: none; letter-spacing: 0; font: 600 12.5px var(--mono); color: var(--text); }
  .doc .body { padding: 4px 16px 16px; overflow-wrap: anywhere; }
  .rawtext { margin: 0; white-space: pre-wrap; word-break: break-word; }
  .toggle {
    background: none; border: 1px solid var(--line); border-radius: 4px; padding: 0 8px; cursor: pointer; font-size: 11px;
  }
  .hist { padding: 8px 16px 12px; border-top: 1px solid var(--line); display: flex; flex-wrap: wrap; gap: 4px; align-items: center; }
  .commits li { padding: 6px 6px; border-bottom: 1px solid var(--line); font-size: 12.5px; }
  .commits li:last-child { border-bottom: 0; }
  @media (max-width: 1100px) {
    .layout { grid-template-columns: 200px minmax(0, 1fr); }
    .commits { grid-column: 1 / -1; }
  }
  @media (max-width: 700px) {
    .layout { grid-template-columns: minmax(0, 1fr); }
    .files ul { display: flex; flex-wrap: wrap; gap: 2px; }
    .files li { flex: 0 0 auto; }
    .files button { width: auto; }
  }
</style>
