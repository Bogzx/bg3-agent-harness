import { mount } from "svelte";
import App from "./App.svelte";
import "./app.css";
import { setToken } from "./lib/api";
import { resolveToken } from "./lib/token";

const meta = document.querySelector<HTMLMetaElement>('meta[name="bg3bench-panel-token"]')?.content;
setToken(resolveToken(meta, location.search, sessionStorage));
if (new URLSearchParams(location.search).has("token")) history.replaceState(history.state, "", location.pathname + location.hash);

const target = document.getElementById("app");
if (!target) throw new Error("#app missing");
export default mount(App, { target });
