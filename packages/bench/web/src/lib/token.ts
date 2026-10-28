// The panel's write token. bg3bench-serve puts it into index.html's <meta name="bg3bench-panel-token">; every write
// sends it as X-Panel-Token. Under `npm run dev` the page isn't served by the panel, so the slot is left unfilled:
// open the dev page once with ?token=<~/.config/bg3bench/panel-token> and the tab keeps it.

export const SLOT = "__BG3BENCH_PANEL_TOKEN__";
export const HEADER = "X-Panel-Token";
export const DEV_KEY = "bg3bench.panel-token";

type Store = Pick<Storage, "getItem" | "setItem">;

/** The token from the served meta tag, else from ?token= (kept in `store`), else the one `store` holds. */
export function resolveToken(meta: string | null | undefined, search: string, store: Store): string | null {
  if (meta && meta !== SLOT) return meta;
  const fromQuery = new URLSearchParams(search).get("token");
  if (fromQuery) store.setItem(DEV_KEY, fromQuery);
  return fromQuery || store.getItem(DEV_KEY);
}
