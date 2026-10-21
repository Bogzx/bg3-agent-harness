<script lang="ts">
  import { onMount } from "svelte";
  import { api } from "../lib/api";
  import { date } from "../lib/format";
  import { groupFiles, renderMarkdown, type Folder } from "../lib/memory";
  import type { Commit, DiffLine } from "../lib/types";

  let { pt, file, onOpen }: { pt: string; file: string | null; onOpen: (path: string) => void } = $props();

  let folders = $state<Folder[]>([]);
  let commits = $state<Commit[]>([]);
  let error = $state<string | null>(null);
  let doc = $state<{ path: string; text: string; history: Commit[] } | null>(null);
  let docError = $state<string | null>(null);
  let raw = $state(false);
  let diff = $state<{ sha: string; lines: DiffLine[] } | null>(null);
  let diffError = $state<string | null>(null);

  function showDiff(c: Commit) {
    if (!doc) return;
    if (diff?.sha === c.sha) return void (diff = null);
    const path = doc.path;
    diffError = null;
    api
      .memoryDiff(pt, path, c.sha)
      .then((d) => {
        if (path === current) diff = { sha: c.sha, lines: d.lines.filter((l) => l.kind !== "meta") };
      })
      .catch((e: Error) => (diffError = e.message));
  }

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
    diff = null;
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
      {:else if diffError}
        <div class="error">{diffError}</div>
      {:else if diff}
        {@const c = doc?.history.find((h) => h.sha === diff?.sha)}
        <div class="diffhead faint">{c ? `${c.subject} · ${date(c.t)}` : diff.sha.slice(0, 7)}
          <button class="toggle" onclick={() => (diff = null)}>back to the file</button></div>
        {#if diff.lines.length}
          <pre class="body diff">{#each diff.lines as l, i (i)}<span class={l.kind}>{l.text}</span>{"\n"}{/each}</pre>
        {:else}
          <div class="empty">This commit didn't change {current}.</div>
        {/if}
      {:else if !doc}
        <div class="empty">Loading…</div>
      {:else if html !== null}
        <div class="body md">{@html html}</div>
      {:else}
        <pre class="body rawtext">{doc.text}</pre>
      {/if}
      {#if doc?.history.length}
        <div class="hist faint">
          Edited in (click for the change):
          {#each doc.history as c (c.sha)}
            <button class="chip" class:on={diff?.sha === c.sha} title="{c.subject} · {date(c.t)}" onclick={() => showDiff(c)}>
              {c.session ?? c.sha.slice(0, 7)}
            </button>
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
  .hist button.chip { cursor: pointer; font: inherit; font-size: 12px; }
  .hist button.chip.on { color: var(--text); border-color: var(--accent); }
  .diffhead { display: flex; gap: 8px; align-items: center; padding: 8px 16px 0; font-size: 12.5px; flex-wrap: wrap; }
  .diff { margin: 0; white-space: pre-wrap; word-break: break-word; font-size: 12.5px; }
  .diff .add { color: var(--ok); background: color-mix(in srgb, var(--ok) 10%, transparent); }
  .diff .del { color: var(--bad); background: color-mix(in srgb, var(--bad) 10%, transparent); }
  .diff .hunk { color: var(--info); }
  .diff .ctx { color: var(--muted); }
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
