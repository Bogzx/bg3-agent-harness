// The server's --token, which writes (Start, Resume, Stop) need from off localhost. Open the app once as
// http://<host>:<port>/?token=<token>#/launch: the tab keeps it (sessionStorage), the address bar loses it, and every
// POST carries it as a bearer. Reads never need it.

export const TOKEN_KEY = "bg3play.token";

type Store = Pick<Storage, "getItem" | "setItem">;

/** The `?token=` in `search` saved to `store`; returns `search` without it (null: it had none). */
export function takeToken(search: string, store: Store): string | null {
  const q = new URLSearchParams(search);
  const token = q.get("token");
  if (token === null) return null;
  if (token) store.setItem(TOKEN_KEY, token);
  q.delete("token");
  const rest = q.toString();
  return rest ? `?${rest}` : "";
}

/** The Authorization header for a write: the saved token, or none. */
export function authHeaders(store: Pick<Storage, "getItem">): Record<string, string> {
  const token = store.getItem(TOKEN_KEY);
  return token ? { Authorization: `Bearer ${token}` } : {};
}
