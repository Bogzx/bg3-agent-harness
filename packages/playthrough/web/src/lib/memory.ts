// The memory browser's file tree and a small, safe Markdown renderer (escape first, then a few block/inline rules).
import type { MemoryFile } from "./types";

// The files the agent reads first, in the order a viewer wants them; the rest follow by name.
const PINNED = ["PLAYTHROUGH.md", "HANDOFF.md", "CHARACTER.md", "objectives.md", "decisions.md", "quests.md"];

export interface Folder {
  name: string;
  files: MemoryFile[];
}

export function groupFiles(files: MemoryFile[]): Folder[] {
  const top: MemoryFile[] = [];
  const dirs = new Map<string, MemoryFile[]>();
  for (const f of files) {
    const i = f.path.indexOf("/");
    if (i < 0) top.push(f);
    else {
      const d = f.path.slice(0, i);
      dirs.set(d, [...(dirs.get(d) ?? []), f]);
    }
  }
  const rank = (p: string) => {
    const i = PINNED.indexOf(p);
    return i < 0 ? PINNED.length : i;
  };
  top.sort((a, b) => rank(a.path) - rank(b.path) || a.path.localeCompare(b.path));
  const out: Folder[] = [{ name: "", files: top }];
  for (const name of [...dirs.keys()].sort()) {
    const fs = dirs.get(name) ?? [];
    // session logs and chapters read newest first
    fs.sort((a, b) => (name === "sessions" || name === "chapters" ? b.path.localeCompare(a.path) : a.path.localeCompare(b.path)));
    out.push({ name, files: fs });
  }
  return out;
}

export function escapeHtml(s: string): string {
  return s.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c] ?? c);
}

function inline(s: string): string {
  return escapeHtml(s)
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/\[(seen [^\]]*|wiki|guess)\]/g, '<span class="tag">[$1]</span>');
}

/** Markdown -> HTML for memory files: headings, lists, quotes, fences, paragraphs; HTML comments dropped; every
 * character of the source is escaped, so a file can't inject markup. */
export function renderMarkdown(md: string): string {
  const lines = md.replace(/<!--[\s\S]*?-->/g, "").split("\n");
  const out: string[] = [];
  let list: "ul" | "ol" | null = null;
  let para: string[] = [];
  let fence: string[] | null = null;
  const flushPara = () => {
    if (para.length) out.push(`<p>${para.map(inline).join("<br>")}</p>`);
    para = [];
  };
  const closeList = () => {
    if (list) out.push(`</${list}>`);
    list = null;
  };
  for (const line of lines) {
    if (fence) {
      if (line.startsWith("```")) {
        out.push(`<pre><code>${escapeHtml(fence.join("\n"))}</code></pre>`);
        fence = null;
      } else fence.push(line);
      continue;
    }
    if (line.startsWith("```")) {
      flushPara();
      closeList();
      fence = [];
      continue;
    }
    const h = /^(#{1,4})\s+(.*)$/.exec(line);
    const ul = /^\s*[-*]\s+(.*)$/.exec(line);
    const ol = /^\s*\d+\.\s+(.*)$/.exec(line);
    if (h) {
      flushPara();
      closeList();
      const level = (h[1] ?? "#").length;
      out.push(`<h${level}>${inline(h[2] ?? "")}</h${level}>`);
    } else if (ul || ol) {
      flushPara();
      const kind = ul ? "ul" : "ol";
      if (list !== kind) {
        closeList();
        out.push(`<${kind}>`);
        list = kind;
      }
      out.push(`<li>${inline((ul ?? ol)?.[1] ?? "")}</li>`);
    } else if (line.startsWith(">")) {
      flushPara();
      closeList();
      out.push(`<blockquote>${inline(line.replace(/^>\s?/, ""))}</blockquote>`);
    } else if (!line.trim()) {
      flushPara();
      closeList();
    } else {
      closeList();
      para.push(line);
    }
  }
  if (fence) out.push(`<pre><code>${escapeHtml(fence.join("\n"))}</code></pre>`);
  flushPara();
  closeList();
  return out.join("\n");
}
