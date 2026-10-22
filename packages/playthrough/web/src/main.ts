import { mount } from "svelte";
import App from "./App.svelte";
import Overlay from "./Overlay.svelte";
import "./app.css";
import { takeToken } from "./lib/auth";

// ?token= (the server's --token for writes from off localhost) goes to this tab's storage, not the history
const search = takeToken(location.search, sessionStorage);
if (search !== null) history.replaceState(history.state, "", `${location.pathname}${search}${location.hash}`);

const target = document.getElementById("app");
if (!target) throw new Error("#app missing");
// /overlay is the transparent stream overlay (served the same index.html); everything else is the app
export default mount(location.pathname.replace(/\/+$/, "") === "/overlay" ? Overlay : App, { target });
