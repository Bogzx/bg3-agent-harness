## Tool names in Claude Code
**The real names of the game tools are `mcp__bg3__<name>`** (`mcp__bg3__get_status`, `mcp__bg3__travel_to`): this
file shortens them to the bare name, but a call to the bare name fails with "No such tool available". The core ones
(`queue_actions`, `do_actions`, `wait_for`, `travel_to`, `move_to`, `interact`, `look`, `combat_turn`, …) are loaded
from the start, and so are `screenshot`, `click`, `press_key`, `dismiss_popups`, `lockpick`, `regroup`, `get_map`, the
rests, `camp` and `level_up`: call them directly. Load others with ToolSearch only when you need one: `ToolSearch` is
Claude Code's own tool, the one name here WITHOUT the prefix (`mcp__bg3__ToolSearch` does not exist), e.g. ToolSearch
with query `select:mcp__bg3__get_layout`.
