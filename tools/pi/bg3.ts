// The one extension the harness runs pi with (`pi -ne -na -e bg3.ts`; research/arch/pi-client.md §3.2).
//
// - Connects to the bg3 MCP server itself and registers each of its tools as `mcp__bg3__<name>` with
//   executionMode "sequential": pi runs a message's tool calls in parallel unless a tool says otherwise, and the
//   bg3 server runs concurrent calls concurrently (parallel calls = parallel game input). pi's built-in MCP has no
//   such setting, so it is not used.
// - Sets the system text verbatim from the file the harness assembles (D2): `--system-prompt` would append a
//   <cwd> section, the before_agent_start override does not.
// - Records each provider response's id, served provider and raw usage (OpenRouter's billed `usage.cost`) as a
//   `bg3.billing` session entry; it reaches the runner as `entry_appended` on the JSON stream. `bg3.tools` records
//   how many tools the server listed, for the runner's tool-count check.
//
// Config: the JSON file named by BG3_PI_CONFIG (runner/pi_client.py writes it):
//   {"server": {"command", "args", "env", "cwd", "timeout"} | {"url", "timeout"}, "systemPromptFile": "<path>"}
// timeout is in seconds.
import { readFileSync } from "node:fs";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { McpClient, StdioTransport, StreamableHttpTransport, toLlmContent } from "@earendil-works/pi-mcp";

interface ServerConfig {
	command?: string;
	args?: string[];
	env?: Record<string, string>;
	cwd?: string;
	url?: string;
	timeout?: number;
}

interface Bg3Config {
	server: ServerConfig;
	systemPromptFile: string;
}

const PREFIX = "mcp__bg3__";

export default async function (pi: ExtensionAPI) {
	const path = process.env.BG3_PI_CONFIG;
	if (!path) throw new Error("bg3.ts: BG3_PI_CONFIG is not set");
	const cfg: Bg3Config = JSON.parse(readFileSync(path, "utf8"));
	if (!cfg.systemPromptFile) throw new Error("bg3.ts: systemPromptFile is not set");
	const system = readFileSync(cfg.systemPromptFile, "utf8");
	const s = cfg.server;
	const client = new McpClient({ name: "bg3-agent-harness", version: "0" });
	await client.connect(
		s.url
			? new StreamableHttpTransport({ url: s.url })
			: new StdioTransport({ command: s.command!, args: s.args ?? [], env: s.env ?? {}, cwd: s.cwd, stderr: "inherit" }),
	);
	const timeoutMs = (s.timeout ?? 110) * 1000;
	const tools = await client.listTools();
	for (const tool of tools) {
		const schema = tool.inputSchema as Record<string, unknown>;
		pi.registerTool({
			name: PREFIX + tool.name,
			label: `bg3/${tool.name}`,
			description: tool.description ?? tool.name,
			parameters: { ...schema, type: "object", properties: schema.properties ?? {} } as never,
			executionMode: "sequential",
			async execute(_id, params, signal) {
				const r = await client.callTool(tool.name, (params ?? {}) as Record<string, unknown>, { signal, timeoutMs });
				return { content: toLlmContent(r) as never, details: undefined, isError: r.isError === true };
			},
		});
	}
	pi.on("session_shutdown", async () => client.close());
	let toolsRecorded = false;
	pi.on("before_agent_start", async () => {
		// once per process, here rather than at session_start: entries appended before the run starts do not reach
		// the JSON stream
		if (!toolsRecorded) pi.appendEntry("bg3.tools", { n: tools.length, names: tools.map((t) => t.name) });
		toolsRecorded = true;
		return { systemPrompt: system };
	});
	pi.on("provider_stream_event", async (ev) => {
		const d = ev.data as Record<string, unknown> | null;
		if (d && typeof d === "object" && d.usage) {
			pi.appendEntry("bg3.billing", { id: d.id, provider: d.provider, model: d.model, usage: d.usage });
		}
	});
}
