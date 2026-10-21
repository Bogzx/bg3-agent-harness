<script lang="ts">
  import { onMount } from "svelte";
  import { api, openStream } from "../lib/api";
  import { actions, DIFFICULTIES, formFrom, launchBody, profileLabel, type Action, type LaunchForm } from "../lib/launcher";
  import type { ClientProfile, SupervisorView } from "../lib/types";

  let { pt, known, onWatch }: { pt: string | null; known: string[]; onWatch: (name: string) => void } = $props();

  let sup = $state.raw<SupervisorView | null>(null);
  let enabled = $state(true); // the server runs a supervisor (--supervise)
  let profiles = $state<ClientProfile[]>([]);
  let form = $state<LaunchForm>({ name: "", profile: "", brief: "", difficulty: "Explorer", minutes: 60, maxCost: 0, recording: true });
  let sessions = $state(0);
  let error = $state<string | null>(null);
  let busy = $state(false);
  let loaded = ""; // the name whose config fills the form: a second change event must not wipe the user's picks

  async function loadConfig(name: string) {
    if (!name || name === loaded) return;
    loaded = name;
    try {
      const c = await api.config(name);
      form = formFrom(c.config);
      sessions = c.sessions;
      error = null;
    } catch (e) {
      error = (e as Error).message;
    }
  }

  onMount(() => {
    api.supervisor().then(
      (r) => {
        sup = r.supervisor;
        enabled = r.supervisor !== null;
        profiles = r.profiles;
      },
      (e: Error) => (error = e.message),
    );
    form.name = pt ?? "";
    void loadConfig(form.name);
    return openStream(null, (type, data) => {
      if (type === "supervisor") sup = data as SupervisorView;
    }, () => {}, () => {});
  });

  const can = $derived(actions(form.name, sessions, sup));
  const picked = $derived(profiles.find((p) => p.name === form.profile) ?? null);

  async function act(what: Action) {
    const b = launchBody(form, profiles);
    if ("error" in b) {
      error = b.error;
      return;
    }
    busy = true;
    try {
      const r = await api.control(form.name, what, what === "stop" ? {} : b.body);
      sup = r.supervisor;
      error = null;
      if (what === "resume") onWatch(form.name); // a new playthrough has nothing to watch until its runner writes
    } catch (e) {
      error = (e as Error).message;
    } finally {
      busy = false;
    }
  }
</script>

<section class="panel launch">
  <h2>Launch a playthrough
    <span class="right state" class:run={sup?.state === "running"}>
      {#if !enabled}supervisor off{:else if sup}{sup.state}{sup.playthrough ? ` · ${sup.playthrough}` : ""}{sup.session ? ` · ${sup.session}` : ""}{sup.detail ? ` · ${sup.detail}` : ""}{/if}
    </span>
  </h2>
  <form class="body" onsubmit={(e) => e.preventDefault()}>
    {#if !enabled}
      <p class="note">This server runs without <code>--supervise</code>: start, stop and resume are off. You can still edit
        and check the settings.</p>
    {/if}
    <label class="field">
      <span>Name</span>
      <input list="pts" bind:value={form.name} onchange={() => loadConfig(form.name)} placeholder="ilse" autocomplete="off" />
      <datalist id="pts">{#each known as k (k)}<option value={k}></option>{/each}</datalist>
      <small class="faint">{sessions ? `${sessions} sessions so far: Resume plays the next one` : "new: Start plays session 1"}</small>
    </label>
    <label class="field">
      <span>Client profile</span>
      <select bind:value={form.profile}>
        <option value="">from playthrough.toml (client and model)</option>
        {#each profiles as p (p.name)}<option value={p.name} disabled={p.error !== null}>{profileLabel(p)}</option>{/each}
      </select>
      {#if picked?.client === "pi"}
        <small class="faint">pi plays through {picked.provider}; the key comes from ${picked.api_key_env} in the machine's
          .env. Needs a cost cap; the memory keeper is off for pi sessions.</small>
      {:else if picked}
        <small class="faint">{picked.client} uses its CLI's own login and endpoint.</small>
      {/if}
    </label>
    <label class="field">
      <span>Character brief</span>
      <textarea rows="3" bind:value={form.brief} placeholder="a pious, short-tempered dwarf cleric who distrusts mind flayers"></textarea>
      <small class="faint">Empty: no character phase.</small>
    </label>
    <fieldset class="field">
      <legend>Difficulty</legend>
      <div class="radios">
        {#each DIFFICULTIES as d (d)}
          <label><input type="radio" name="difficulty" value={d} bind:group={form.difficulty} /> {d}</label>
        {/each}
      </div>
    </fieldset>
    <div class="row2">
      <label class="field"><span>Session minutes</span><input type="number" min="1" max="600" bind:value={form.minutes} /></label>
      <label class="field"><span>Cost cap (USD)</span><input type="number" min="0" step="0.5" bind:value={form.maxCost} />
        <small class="faint">{picked?.client === "pi" ? "required" : "0 = none"}</small></label>
      <label class="check"><input type="checkbox" bind:checked={form.recording} /> Record video</label>
    </div>
    {#if error}<div class="error">{error}</div>{/if}
    <div class="buttons">
      {#each can as a (a)}
        <button class="btn" class:primary={a !== "stop"} class:danger={a === "stop"} disabled={busy || !enabled} onclick={() => act(a)}>
          {a === "start" ? "Start" : a === "resume" ? "Resume" : "Stop after this session"}
        </button>
      {/each}
      {#if enabled && !can.length && sup && sup.state !== "idle"}
        <span class="faint">{sup.playthrough} is {sup.state}; one playthrough runs at a time.</span>
      {/if}
    </div>
  </form>
</section>

<style>
  .launch { max-width: 720px; margin: 0 auto; }
  .body { padding: 12px 16px 16px; display: flex; flex-direction: column; gap: 14px; }
  .field { display: flex; flex-direction: column; gap: 4px; border: 0; padding: 0; margin: 0; }
  .field > span, legend { font-size: 11px; text-transform: uppercase; letter-spacing: 0.06em; color: var(--muted); padding: 0; }
  input:not([type="radio"]):not([type="checkbox"]), select, textarea {
    background: var(--panel-2); border: 1px solid var(--line); border-radius: 6px; padding: 6px 8px; color: var(--text);
    font: inherit; width: 100%;
  }
  textarea { resize: vertical; }
  .radios { display: flex; flex-wrap: wrap; gap: 6px 16px; }
  .row2 { display: flex; flex-wrap: wrap; gap: 12px 24px; align-items: end; }
  .row2 .field { width: 160px; }
  .check { display: flex; gap: 6px; align-items: center; padding-bottom: 6px; }
  .buttons { display: flex; flex-wrap: wrap; gap: 10px; align-items: center; }
  .btn { border: 1px solid var(--line); background: var(--panel-2); border-radius: 6px; padding: 7px 16px; cursor: pointer; font-weight: 600; }
  .btn.primary { background: var(--accent); border-color: var(--accent); color: #111; }
  .btn.danger { border-color: var(--bad); color: var(--bad); }
  .btn:disabled { opacity: 0.5; cursor: default; }
  .state { font-size: 12px; color: var(--muted); text-transform: none; letter-spacing: 0; }
  .state.run { color: var(--ok); }
  .note { margin: 0; color: var(--muted); font-size: 13px; }
</style>
