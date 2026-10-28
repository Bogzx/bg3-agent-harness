<script lang="ts">
  // API keys (to the repo's .env, shown masked: the panel never sends a key back), the site token (its own file),
  // and the YouTube question.
  import { onMount } from "svelte";
  import { api } from "../lib/api";
  import type { SettingsView } from "../lib/types";

  let data = $state<SettingsView | null>(null);
  let error = $state<string | null>(null);
  let msg = $state<string | null>(null);
  let drafts = $state<Record<string, string>>({});
  let siteToken = $state("");

  async function load() {
    try {
      data = await api.settings();
      error = null;
    } catch (e) {
      error = (e as Error).message;
    }
  }

  async function act(f: () => Promise<unknown>, ok: string) {
    msg = null;
    try {
      await f();
      msg = ok;
      await load();
    } catch (e) {
      msg = (e as Error).message;
    }
  }

  function saveKey(name: string) {
    const value = drafts[name] ?? "";
    void act(() => api.setKey(name, value), `${name} saved`).then(() => (drafts[name] = ""));
  }

  onMount(load);
</script>

{#if error}
  <div class="error">{error}</div>
{:else if !data}
  <div class="empty">Loading…</div>
{:else}
  {#if msg}<div class="note">{msg}</div>{/if}
  <section class="panel">
    <h2>API keys <span class="right faint">{data.dotenv}</span></h2>
    <div class="body">
      {#if !data.dotenv_ignored}
        <div class="note bad">This .env is not ignored by git: add <code>.env</code> to .gitignore before saving a key.</div>
      {/if}
      {#each data.keys as k (k.name)}
        <label class="row">
          <b>{k.name}</b>
          <span class="muted">{k.masked ?? "not set"}</span>
          <input type="password" autocomplete="off" placeholder="new value" bind:value={drafts[k.name]} />
          <button class="plain" disabled={!drafts[k.name]} onclick={() => saveKey(k.name)}>Save</button>
        </label>
      {:else}
        <div class="empty">No profile names an API key.</div>
      {/each}
    </div>
  </section>

  <section class="panel">
    <h2>Site token</h2>
    <div class="body">
      <p class="muted">Log in on the site with GitHub, make a panel token there and paste it here once. It is kept in
        its own file, outside the repo.</p>
      <label class="row">
        <span class="muted">{data.site_token ?? "not set"}</span>
        <input type="password" autocomplete="off" placeholder="token" bind:value={siteToken} />
        <button class="plain" disabled={!siteToken}
          onclick={() => act(() => api.setSiteToken(siteToken), "site token saved").then(() => (siteToken = ""))}>Save</button>
      </label>
    </div>
  </section>

  <section class="panel">
    <h2>Evidence video</h2>
    <div class="body">
      <p>Is your YouTube account verified for long uploads? Without phone verification YouTube takes videos of up to
        15 minutes only, and most runs are longer.</p>
      <label class="row"><input type="radio" name="yt" checked={data.prefs.youtube_verified === true}
        onchange={() => act(() => api.setPrefs({ youtube_verified: true }), "saved")} /> yes, verified</label>
      <label class="row"><input type="radio" name="yt" checked={data.prefs.youtube_verified === false}
        onchange={() => act(() => api.setPrefs({ youtube_verified: false }), "saved")} /> no</label>
      <label class="row">Disk cap for recordings (GB)
        <input type="number" min="1" value={data.prefs.record_cap_gb}
          onchange={(e) => act(() => api.setPrefs({ record_cap_gb: Number((e.currentTarget as HTMLInputElement).value) }), "saved")} /></label>
    </div>
  </section>
{/if}
