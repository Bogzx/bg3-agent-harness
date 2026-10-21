import { mount } from "svelte";
import App from "./App.svelte";
import Overlay from "./Overlay.svelte";
import "./app.css";

const target = document.getElementById("app");
if (!target) throw new Error("#app missing");
// /overlay is the transparent stream overlay (served the same index.html); everything else is the app
export default mount(location.pathname.replace(/\/+$/, "") === "/overlay" ? Overlay : App, { target });
