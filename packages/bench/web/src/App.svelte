<script lang="ts">
  import { onMount } from "svelte";
  import { LABELS, parseRoute, SCREENS, type Screen } from "./lib/route";
  import About from "./screens/About.svelte";
  import Doctor from "./screens/Doctor.svelte";
  import NewRun from "./screens/NewRun.svelte";
  import Preflight from "./screens/Preflight.svelte";
  import Queue from "./screens/Queue.svelte";
  import Results from "./screens/Results.svelte";
  import Saves from "./screens/Saves.svelte";
  import Videos from "./screens/Videos.svelte";
  import Settings from "./screens/Settings.svelte";

  let screen = $state<Screen>(parseRoute(location.hash));

  $effect(() => {
    document.title = `${LABELS[screen]} · BG3 bench panel`;
  });

  onMount(() => {
    const onHash = () => (screen = parseRoute(location.hash));
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  });
</script>

<header>
  <div class="brand"><span class="mark">BG3</span> bench</div>
  <nav aria-label="Screens">
    {#each SCREENS as s (s)}
      <a class:active={screen === s} aria-current={screen === s ? "page" : undefined} href="#/{s}">{LABELS[s]}</a>
    {/each}
  </nav>
</header>

<main>
  <h1 class="sr-only">{LABELS[screen]}</h1>
  {#if screen === "preflight"}
    <Preflight />
  {:else if screen === "doctor"}
    <Doctor />
  {:else if screen === "saves"}
    <Saves />
  {:else if screen === "new"}
    <NewRun />
  {:else if screen === "queue"}
    <Queue />
  {:else if screen === "results"}
    <Results />
  {:else if screen === "videos"}
    <Videos />
  {:else if screen === "settings"}
    <Settings />
  {:else}
    <About />
  {/if}
</main>

<style>
  header {
    position: sticky; top: 0; z-index: 5; display: flex; flex-wrap: wrap; align-items: center; gap: 8px 16px;
    padding: 8px 16px; background: var(--header-bg); backdrop-filter: blur(6px); border-bottom: 1px solid var(--line);
  }
  .brand { font-weight: 650; letter-spacing: 0.03em; white-space: nowrap; }
  .mark { color: var(--accent); }
  nav { display: flex; gap: 4px; min-width: 0; max-width: 100%; overflow-x: auto; scrollbar-width: thin; }
  nav a { white-space: nowrap; }
  nav a { padding: 4px 10px; border-radius: 6px; color: var(--muted); text-decoration: none; font-weight: 550; }
  nav a:hover { color: var(--text); background: var(--panel-2); }
  nav a.active { color: var(--text); background: var(--panel-2); box-shadow: inset 0 -2px 0 var(--accent); }
  main { padding: 12px 16px 32px; max-width: 1300px; margin: 0 auto; }
</style>
