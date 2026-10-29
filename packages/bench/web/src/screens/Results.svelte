<script lang="ts">
  // Finished runs on this machine (result.json + manifest.json): verdict or score, time, cost, validity, the
  // attempt number within the run's cell, and the size of its kept saves. Validate packs and checks a run locally and
  // shows the verdict and the exact files before Upload sends them; Render video makes the evidence video.
  import { onMount } from "svelte";
  import TableWrap from "../lib/TableWrap.svelte";
  import { api } from "../lib/api";
  import { usd } from "../lib/format";
  import { attempts, bytes, minutes, renderNote, uploadProblem, validateProblem, verdict } from "../lib/run";
  import type { Prepared, RenderState, RunRow, Submission } from "../lib/types";

  let rows = $state<RunRow[] | null>(null);
  let error = $state<string | null>(null);
  const n = $derived(attempts(rows ?? []));
  let subs = $state<Record<string, { prepared: Prepared | null; submission: Submission | null }>>({});
  let siteToken = $state(false);
  let render = $state<RenderState | null>(null);
  let open = $state<string | null>(null); // the run dir whose Submit box is open
  let youtubeUrl = $state("");
  let busy = $state(false);
  let actionError = $state<string | null>(null);

  const prepared = $derived(open ? (subs[open]?.prepared ?? null) : null);
  const submission = $derived(open ? (subs[open]?.submission ?? null) : null);
  const uploadWhy = $derived(uploadProblem(prepared, youtubeUrl, siteToken));

  function loadSubs() {
    Promise.all([api.submissions(), api.settings(), api.renderState()])
      .then(([s, st, r]) => {
        subs = s.submissions;
        siteToken = st.site_token !== null;
        render = r.render;
      })
      .catch((e: Error) => (actionError = e.message));
  }

  onMount(() => {
    api.results().then((r) => (rows = r.runs)).catch((e: Error) => (error = e.message));
    loadSubs();
    const t = setInterval(loadSubs, 5000); // a finished render changes the bundle (its hash and files)
    return () => clearInterval(t);
  });

  async function act(fn: () => Promise<unknown>) {
    busy = true;
    actionError = null;
    try {
      await fn();
    } catch (e) {
      actionError = (e as Error).message;
    } finally {
      busy = false;
      loadSubs();
    }
  }

  const validate = (dir: string) => act(async () => {
    open = dir;
    const p = await api.prepare(dir);
    subs = { ...subs, [dir]: { prepared: p, submission: null } };
  });
  const upload = () => act(() => api.upload(open!, prepared!.bundle_sha256, youtubeUrl));
  const refresh = () => act(() => api.submissionStatus(open!));
  const doRender = (dir: string) => act(async () => (render = (await api.render(dir)).render));
</script>

<section class="panel">
  <h2>Results</h2>
  {#if error}
    <div class="error">{error}</div>
  {:else if !rows}
    <div class="empty">Loading…</div>
  {:else if !rows.length}
    <div class="empty">No finished run yet.</div>
  {:else}
    <TableWrap label="Finished runs">
      <table>
        <thead><tr><th>run</th><th>scenario</th><th>model</th><th>verdict</th><th class="num">wall</th>
          <th class="num">cost</th><th class="num">luck z</th><th class="num">attempt</th><th class="num">saves</th><th></th></tr></thead>
        <tbody>
          {#each rows as r (r.dir)}
            {@const a = n.get(r.dir)}
            {@const why = validateProblem(r)}
            <tr>
              <td><code>{r.run_id ?? r.dir}</code>{#if r.pilot.length}<div class="faint">pilot</div>{/if}</td>
              <td>{r.scenario ?? "–"}</td>
              <td>{r.model ?? "–"}<div class="faint">{r.client ?? ""} · {r.condition ?? ""}</div></td>
              <td><span class="st {r.valid === false ? 'FAIL' : r.passed === true ? 'PASS' : 'none'}">{verdict(r)}</span>
                {#if r.t_pass_s !== null}<div class="faint">at {minutes(r.t_pass_s)}</div>{/if}</td>
              <td class="num">{minutes(r.wall_s)}</td>
              <td class="num">{usd(r.cost_usd)}</td>
              <td class="num">{r.luck_z === null ? "–" : r.luck_z.toFixed(2)}</td>
              <td class="num">{a ? `${a[0]} of ${a[1]}` : "–"}</td>
              <td class="num">{r.saves_bytes ? bytes(r.saves_bytes) : "–"}</td>
              <td>
                {#if subs[r.dir]?.submission}
                  <button class="plain" onclick={() => (open = r.dir)}>{subs[r.dir]?.submission?.status}</button>
                {:else if why}
                  <button class="plain" disabled title={why}>Validate</button><div class="faint">{why}</div>
                {:else}
                  <button class="plain" disabled={busy} onclick={() => validate(r.dir)}>Validate</button>
                {/if}
              </td>
            </tr>
          {/each}
        </tbody>
      </table>
    </TableWrap>
  {/if}
  {#if actionError}<div class="note bad">{actionError}</div>{/if}
</section>

{#if open}
  <section class="panel" aria-label="Submit">
    <h2>Submit {open} <button class="plain right" onclick={() => (open = null)}>Close</button></h2>
    <div class="body">
      {#if submission}
        <p>Submission <code>{submission.id}</code>: <b>{submission.status}</b>
          {#if submission.flags?.length}<span class="faint">({submission.flags.join(", ")})</span>{/if}
          {#if submission.run_url}· <a href={submission.run_url} target="_blank" rel="noreferrer">run page</a>{/if}</p>
        {#each (submission.checks ?? []).filter((c) => c.ok === false) as c (c.code)}
          <div class="faint">{c.code}: {c.detail ?? ""}</div>
        {/each}
        <button class="plain" disabled={busy} onclick={refresh}>Refresh status</button>
      {:else if prepared}
        <p>Bundle <code>{prepared.bundle_sha256.slice(0, 12)}</code> · attempt {prepared.attempt.n} of its cell
          {#if prepared.attempt.unsubmitted_valid}· {prepared.attempt.unsubmitted_valid} earlier valid run(s) of the
            cell not submitted (submit every valid run of a cell, or none){/if}</p>
        <p><span class="st {prepared.verdict.accepted ? 'PASS' : 'FAIL'}">{prepared.verdict.accepted ? "accepted" :
          "rejected"}</span> by the local validator{prepared.final ? "" : " (the record only: render the video for the final check)"}</p>
        {#each prepared.verdict.checks.filter((c) => c.ok === false) as c (c.code)}
          <div class="faint">{c.code} ({c.severity ?? "?"}): {c.detail ?? ""}</div>
        {/each}
        <TableWrap label="Bundle files">
          <table aria-label="Files to upload">
            <thead><tr><th>file</th><th class="num">size</th></tr></thead>
            <tbody>
              {#each prepared.files as f (f.name)}<tr><td><code>{f.name}</code></td><td class="num">{bytes(f.bytes)}</td></tr>{/each}
            </tbody>
          </table>
        </TableWrap>
        <div class="row">
          <button class="plain" disabled={busy || (render?.running ?? false)} onclick={() => doRender(open!)}>Render video</button>
          <span class="faint">expect 1-2 GB per hour of run; upload it to YouTube yourself (public or unlisted)</span>
        </div>
        {#if render && render.run === open}
          <div class="faint">{render.running ? "rendering…" : render.video ?
            `video ${render.video} (${bytes(render.video_bytes ?? 0)}); ${renderNote(prepared?.render, render.out)}` :
            `render ended (exit ${render.returncode ?? "?"}); see ${render.log}`}</div>
        {/if}
        <label class="row">YouTube URL <input type="url" bind:value={youtubeUrl} placeholder="https://youtu.be/…" size="40" /></label>
        <div class="row">
          <button class="primary" disabled={busy || uploadWhy !== null} onclick={upload}>Upload</button>
          {#if uploadWhy}<span class="faint">{uploadWhy}</span>{/if}
          <button class="plain" disabled={busy} onclick={() => validate(open!)}>Validate again</button>
        </div>
      {:else}
        <div class="empty">{busy ? "Packing and validating…" : "Not validated yet."}</div>
      {/if}
    </div>
  </section>
{/if}
