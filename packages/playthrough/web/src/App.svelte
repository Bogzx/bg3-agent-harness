<script lang="ts">
  import { onMount } from "svelte";
  import { api } from "./lib/api";
  import { href, parseRoute, SCREENS, type Route } from "./lib/route";
  import type { PlaythroughRow } from "./lib/types";
  import Launch from "./screens/Launch.svelte";
  import Memory from "./screens/Memory.svelte";
  import Replay from "./screens/Replay.svelte";
  import Timeline from "./screens/Timeline.svelte";
  import Watch from "./screens/Watch.svelte";

  type Theme = "" | "dark" | "light";
  const LABELS = { watch: "Watch", memory: "Memory", timeline: "Timeline", replay: "Replay", launch: "Launch" };

  let route = $state<Route>(parseRoute(location.hash));
  let pts = $state<PlaythroughRow[]>([]);
  let error = $state<string | null>(null);
  let theme = $state<Theme>(initialTheme());

  function initialTheme(): Theme {
    const q = new URLSearchParams(location.search).get("theme");
    if (q === "dark" || q === "light") return q;
    try {
      const saved = localStorage.getItem("bg3play.theme");
      return saved === "dark" || saved === "light" ? saved : "";
    } catch {
      return "";
    }
  }

  $effect(() => {
    if (theme) document.documentElement.dataset.theme = theme;
    else delete document.documentElement.dataset.theme;
  });

  function cycleTheme() {
    theme = theme === "" ? "light" : theme === "light" ? "dark" : "";
    try {
      localStorage.setItem("bg3play.theme", theme);
    } catch {
      // storage blocked: the choice lasts this page view
    }
  }

  function go(r: Route) {
    location.hash = href(r);
  }

  onMount(() => {
    const onHash = () => (route = parseRoute(location.hash));
    window.addEventListener("hashchange", onHash);
    api
      .playthroughs()
      .then((p) => {
        pts = p.playthroughs;
        if (!route.pt && route.screen !== "launch") {
          const pick = pts.find((x) => x.current) ?? pts[pts.length - 1];
          if (pick) go({ ...route, pt: pick.name });
        }
      })
      .catch((e: Error) => (error = e.message));
    return () => window.removeEventListener("hashchange", onHash);
  });
</script>

<header>
  <div class="brand"><span class="mark">BG3</span> Playthrough</div>
  <nav>
    {#each SCREENS as s (s)}
      <a class:active={route.screen === s} href={href({ screen: s, pt: route.pt, file: null })}>{LABELS[s]}</a>
    {/each}
  </nav>
  <div class="spacer"></div>
  {#if pts.length}
    <select
      aria-label="Playthrough"
      value={route.pt ?? ""}
      onchange={(e) => go({ screen: route.screen, pt: (e.currentTarget as HTMLSelectElement).value, file: null })}
    >
      {#if !route.pt}<option value="" disabled>playthrough…</option>{/if}
      {#each pts as p (p.name)}
        <option value={p.name}>{p.name}{p.current ? " ●" : ""}</option>
      {/each}
    </select>
  {/if}
  <button class="theme" onclick={cycleTheme} title="Theme: {theme || 'system'}" aria-label="Toggle theme">
    {theme === "dark" ? "Dark" : theme === "light" ? "Light" : "Auto"}
  </button>
</header>

<main>
  {#if error}
    <div class="error">Can't reach the API: {error}</div>
  {:else if route.screen === "launch"}
    <Launch pt={route.pt} known={pts.map((p) => p.name)} onWatch={(name) => go({ screen: "watch", pt: name, file: null })} />
  {:else if !route.pt}
    <div class="empty">{pts.length ? "Pick a playthrough." : "Loading…"}</div>
  {:else}
    {#key route.pt}
      {#if route.screen === "watch"}
        <Watch pt={route.pt} />
      {:else if route.screen === "memory"}
        <Memory pt={route.pt} file={route.file} onOpen={(f) => go({ screen: "memory", pt: route.pt, file: f })} />
      {:else if route.screen === "replay"}
        {#key route.file}
          <Replay pt={route.pt} session={route.file} onSession={(f) => go({ screen: "replay", pt: route.pt, file: f })} />
        {/key}
      {:else}
        <Timeline pt={route.pt} />
      {/if}
    {/key}
  {/if}
</main>

<style>
  header {
    position: sticky; top: 0; z-index: 5; display: flex; flex-wrap: wrap; align-items: center; gap: 8px 16px;
    padding: 8px 16px; background: var(--header-bg); backdrop-filter: blur(6px); border-bottom: 1px solid var(--line);
  }
  .brand { font-weight: 650; letter-spacing: 0.03em; white-space: nowrap; }
  .mark { color: var(--accent); }
  nav { display: flex; gap: 4px; }
  nav a {
    padding: 4px 10px; border-radius: 6px; color: var(--muted); text-decoration: none; font-weight: 550;
  }
  nav a:hover { color: var(--text); background: var(--panel-2); }
  nav a.active { color: var(--text); background: var(--panel-2); box-shadow: inset 0 -2px 0 var(--accent); }
  .spacer { flex: 1; }
  select, .theme {
    background: var(--panel); border: 1px solid var(--line); border-radius: 6px; padding: 4px 8px; max-width: 46vw;
  }
  .theme { cursor: pointer; min-width: 56px; font-size: 12.5px; }
  main { padding: 12px 16px 32px; max-width: 1500px; margin: 0 auto; }
  @media (max-width: 560px) {
    header { padding: 8px 12px; }
    .brand { display: none; }
    main { padding: 10px 10px 24px; }
  }
</style>
