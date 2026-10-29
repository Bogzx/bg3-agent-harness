<script lang="ts">
  // The evidence videos waiting for their YouTube link: every rendered run without one, with the file to upload, its
  // size and the exact title and description (the title holds the run id, which the validator checks). After the
  // uploads, paste the links (one per line, in the list's order or "<run id> <link>") and Add videos runs Add video
  // for each; a refused one is shown and the rest still go through.
  import { onMount } from "svelte";
  import TableWrap from "../lib/TableWrap.svelte";
  import { api } from "../lib/api";
  import { bytes, pairLinks } from "../lib/run";
  import type { VideoResult, VideoTodo } from "../lib/types";

  let todo = $state<VideoTodo[] | null>(null);
  let error = $state<string | null>(null);
  let text = $state("");
  let busy = $state(false);
  let results = $state<VideoResult[]>([]);
  const paired = $derived(pairLinks(todo ?? [], text));

  function load() {
    api.videos().then((r) => { todo = r.videos; error = null; }).catch((e: Error) => (error = e.message));
  }
  onMount(load);

  async function add() {
    busy = true;
    error = null;
    try {
      results = (await api.addVideos(paired.items)).results;
      text = paired.unmatched.join("\n");
    } catch (e) {
      error = (e as Error).message;
    } finally {
      busy = false;
      load();
    }
  }
</script>

{#if error}<div class="note bad">{error}</div>{/if}
{#if todo === null}
  <p class="faint">loading…</p>
{:else if todo.length === 0}
  <p>No rendered run waits for a video link. Render a run's video on the Results screen first.</p>
{:else}
  <p>{todo.length} rendered {todo.length === 1 ? "run waits" : "runs wait"} for a YouTube link. Upload each file with
    exactly this title (it holds the run id) and description, then paste the links below.</p>
  <TableWrap label="Videos to upload">
    <table>
      <thead><tr><th>#</th><th>File</th><th>Size</th><th>Title</th><th>Description</th></tr></thead>
      <tbody>
        {#each todo as t, i (t.run)}
          <tr>
            <td>{i + 1}</td>
            <td><code>{t.path}</code></td>
            <td>{bytes(t.bytes)}</td>
            <td><code>{t.title}</code></td>
            <td>{t.description}</td>
          </tr>
        {/each}
      </tbody>
    </table>
  </TableWrap>
  <p><label>Links, one per line, in the order above (or <code>&lt;run id&gt; &lt;link&gt;</code>)<br />
    <textarea bind:value={text} rows="8" cols="70" placeholder="https://youtu.be/…"></textarea></label></p>
  <p>
    <button class="plain" disabled={busy || paired.items.length === 0} onclick={add}>
      Add videos ({paired.items.length})</button>
    {#if paired.unmatched.length}
      <span class="faint">{paired.unmatched.length} line{paired.unmatched.length === 1 ? "" : "s"} can't be used: not
        a YouTube link, no such run, or more links than runs</span>
    {/if}
  </p>
{/if}
{#if results.length}
  <ul>
    {#each results as r (r.run)}
      <li>{r.run}: {r.ok ? `added ${r.video?.youtube_id}` : r.error}</li>
    {/each}
  </ul>
{/if}
