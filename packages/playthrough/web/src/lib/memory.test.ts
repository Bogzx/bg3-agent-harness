import { describe, expect, it } from "vitest";
import { escapeHtml, groupFiles, renderMarkdown } from "./memory";

const f = (path: string) => ({ path, size: 1, mtime: 1 });

describe("memory tree", () => {
  it("pins the agent's main files first and reads session logs newest first", () => {
    const g = groupFiles([f("lessons.md"), f("HANDOFF.md"), f("sessions/S001.md"), f("sessions/S002.md"),
                          f("PLAYTHROUGH.md"), f("snapshots/status.md")]);
    expect(g.map((x) => x.name)).toEqual(["", "sessions", "snapshots"]);
    expect(g[0]!.files.map((x) => x.path)).toEqual(["PLAYTHROUGH.md", "HANDOFF.md", "lessons.md"]);
    expect(g[1]!.files.map((x) => x.path)).toEqual(["sessions/S002.md", "sessions/S001.md"]);
  });
});

describe("markdown", () => {
  it("renders headings, lists, paragraphs, quotes and fences", () => {
    const html = renderMarkdown("# T\n\n- a **b**\n- `c`\n\n1. one\n\ntext\nmore\n> q\n```\n<x>\n```");
    expect(html).toBe([
      "<h1>T</h1>", "<ul>", "<li>a <strong>b</strong></li>", "<li><code>c</code></li>", "</ul>", "<ol>",
      "<li>one</li>", "</ol>", "<p>text<br>more</p>", "<blockquote>q</blockquote>", "<pre><code>&lt;x&gt;</code></pre>",
    ].join("\n"));
  });

  it("escapes everything and drops comments", () => {
    const html = renderMarkdown('<!-- secret -->\n<img src=x onerror="alert(1)">\n- <script>x</script>');
    expect(html).not.toContain("<img");
    expect(html).not.toContain("<script");
    expect(html).not.toContain("secret");
    expect(escapeHtml(`<a href="x">'&`)).toBe("&lt;a href=&quot;x&quot;&gt;&#39;&amp;");
  });

  it("marks evidence tags", () => {
    expect(renderMarkdown("Kagha left [seen S013]")).toContain('<span class="tag">[seen S013]</span>');
  });
});
