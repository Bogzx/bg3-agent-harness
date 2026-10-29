<script lang="ts">
  // New run: pick profiles, scenarios, the condition and repeats; Plan shows what Start would queue (bg3bench.matrix
  // plan), its budget reserve and the YouTube note, before anything runs. Start plays the same form as a matrix,
  // recording each run when Record is ticked; Submit when done packs and validates each valid run (it never uploads).
  import { onMount } from "svelte";
  import TableWrap from "../lib/TableWrap.svelte";
  import { api } from "../lib/api";
  import { usd } from "../lib/format";
  import { formProblems, minutes, startProblem, youtubeNote } from "../lib/run";
  import type { FoundMatrix, Plan, PlanForm, ProfileRow, RunState, ScenarioRow } from "../lib/types";

  let profiles = $state<ProfileRow[]>([]);
  let scenarios = $state<ScenarioRow[]>([]);
  let error = $state<string | null>(null);
  let planError = $state<string | null>(null);
  let plan = $state<Plan | null>(null);
  let busy = $state(false);
  let form = $state<PlanForm>({
    profiles: [], scenarios: [], condition: "rules", repeats: 1, max_cost_usd: null, allow_unverified: false,
  });
  let record = $state(true);
  let submit = $state(false);
  let running = $state<RunState | null>(null);
  let found = $state<FoundMatrix | null>(null);
  let canStart = $state(false);
  let started = $state<string | null>(null);

  const problems = $derived(formProblems(form, profiles));
  const note = $derived(plan ? youtubeNote(plan.youtube) : null);
  const startWhy = $derived(canStart ? startProblem(plan, !!running?.running || !!found) : "this panel starts no run");

  onMount(() => {
    Promise.all([api.profiles(), api.scenarios(), api.run()])
      .then(([p, s, r]) => {
        profiles = p.profiles;
        scenarios = s.scenarios;
        running = r.run;
        found = r.found;
        canStart = r.can_start;
      })
      .catch((e: Error) => (error = e.message));
  });

  function toggle(list: string[], name: string): string[] {
    return list.includes(name) ? list.filter((x) => x !== name) : [...list, name];
  }

  async function doStart() {
    busy = true;
    planError = null;
    try {
      const r = await api.start({ ...$state.snapshot(form), record, submit_when_done: record && submit });
      running = r.started;
      started = `Started ${r.started?.name ?? "the matrix"} (${r.matrix_file}). Its progress is on the Queue screen.`;
    } catch (e) {
      planError = (e as Error).message;
    } finally {
      busy = false;
    }
  }

  async function doPlan() {
    busy = true;
    planError = null;
    started = null;
    plan = null;
    try {
      plan = await api.plan($state.snapshot(form));
    } catch (e) {
      planError = (e as Error).message;
    } finally {
      busy = false;
    }
  }
</script>

{#if error}
  <div class="error">{error}</div>
{:else}
  <section class="panel">
    <h2>Model</h2>
    <div class="body">
      {#each profiles as p (p.name)}
        <label class="row" title={p.why ?? ""}>
          <input type="checkbox" disabled={!p.offered} checked={form.profiles.includes(p.name)}
            onchange={() => (form.profiles = toggle(form.profiles, p.name))} />
          <b>{p.name}</b>
          <span class="muted">{p.client} · {p.model}{p.effort ? ` · ${p.effort}` : ""}</span>
          <span class="st {p.status === 'verified' ? 'PASS' : 'none'}">{p.status ?? "?"}</span>
          {#if p.api_key_env}<span class="faint">{p.api_key_env}: {p.key_set ? "set" : "not set"}</span>{/if}
          {#if p.why}<span class="faint">{p.why}</span>{/if}
        </label>
      {/each}
      <label class="row"><input type="checkbox" bind:checked={form.allow_unverified} />
        <span class="grow">allow profiles that never played live (status offline)</span></label>
    </div>
  </section>

  <section class="panel">
    <h2>Scenarios <span class="right faint">{form.scenarios.length} picked</span></h2>
    <div class="body cols">
      {#each scenarios as s (s.name)}
        <label class="row" title={s.not_ready ?? s.task ?? ""}>
          <input type="checkbox" disabled={!!s.not_ready} checked={form.scenarios.includes(s.name)}
            onchange={() => (form.scenarios = toggle(form.scenarios, s.name))} />
          <span>{s.name}</span><span class="faint">{minutes(s.cap_s)}</span>
          {#if s.not_ready}<span class="faint">not ready</span>{/if}
        </label>
      {/each}
    </div>
  </section>

  <section class="panel">
    <h2>Run</h2>
    <div class="body">
      <label class="row">Condition <input type="text" bind:value={form.condition} /></label>
      <label class="row">Repeats <input type="number" min="1" max="20" bind:value={form.repeats} /></label>
      <label class="row">Max cost per run (USD)
        <input type="number" min="0" step="0.5"
          value={form.max_cost_usd ?? ""}
          oninput={(e) => {
            const v = (e.currentTarget as HTMLInputElement).value;
            form.max_cost_usd = v === "" ? null : Number(v);
          }} />
        <span class="faint">empty: each scenario's own cap; a paid run without one won't start</span></label>
      <label class="row"><input type="checkbox" bind:checked={record} /> Record the game window (the evidence video)</label>
      <label class="row" title="Packs and validates each valid run as it ends; uploading stays your click in Results">
        <input type="checkbox" bind:checked={submit} disabled={!record} /> Submit when done
        <span class="faint">{record ? "packs and validates each valid run; you upload from Results" : "needs Record"}</span>
      </label>
      {#if problems.length}<div class="faint">{problems.join(" · ")}</div>{/if}
      <div class="row">
        <button class="primary" onclick={doPlan} disabled={busy || problems.length > 0}>{busy ? "Planning…" : "Plan"}</button>
        <button class="plain" onclick={doStart} disabled={busy || startWhy !== null} title={startWhy ?? "Play this plan now"}>Start</button>
        {#if startWhy && plan}<span class="faint">{startWhy}</span>{/if}
      </div>
    </div>
  </section>

  {#if planError}<div class="note bad">{planError}</div>{/if}
  {#if started}<div class="note" role="status">{started}</div>{/if}
  {#if plan}
    <section class="panel" aria-label="Plan">
      <h2>Plan <span class="right">{plan.jobs.length} jobs · reserve {usd(plan.reserve_usd)}</span></h2>
      <div class="body">
        {#if note}<div class="note" role="alert">{note}</div>{/if}
        {#if plan.budget.error}
          <div class="note bad">Budget {plan.budget.file}: {plan.budget.error}. Nothing starts without a budget file.</div>
        {:else}
          <p>Budget: {usd(plan.budget.spent)} of {usd(plan.budget.cap)} spent;
            {plan.fits_budget ? "this plan fits." : "this plan doesn't fit."}</p>
        {/if}
        {#if plan.needs_max_cost.length}<div class="note bad">Give a max cost for {plan.needs_max_cost.join(", ")}.</div>{/if}
        {#each Object.entries(plan.skipped) as [name, why] (name)}<div class="faint">{name}: skipped, {why}</div>{/each}
        {#each Object.entries(plan.gaps) as [prof, gaps] (prof)}
          {#if gaps.length}<div class="faint">{prof}: a run lacks {gaps.join("; ")}</div>{/if}
        {/each}
        {#if !record}<div class="note">Without a recording the run has no evidence video and can't be submitted.</div>{/if}
        <TableWrap label="Planned jobs">
          <table>
            <thead><tr><th>job</th><th>scenario</th><th>model</th><th class="num">repeat</th><th class="num">max cost</th></tr></thead>
            <tbody>
              {#each plan.jobs as j (j.id)}
                <tr><td><code>{j.id}</code></td><td>{j.scenario}</td><td>{j.model}</td><td class="num">{j.repeat}</td>
                  <td class="num">{usd(j.max_cost)}</td></tr>
              {/each}
            </tbody>
          </table>
        </TableWrap>
      </div>
    </section>
  {/if}
{/if}

<style>
  .cols { columns: 3 260px; }
  .cols label { break-inside: avoid; margin: 2px 0; }
</style>
