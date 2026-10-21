"""The bg3 MCP server's tools, split out of server.py by group. Each module refers to another module's names as
<module>.<name>, so a test that replaces one reaches every caller. Importing any of them loads them all, in the
MODULES order: the order the tools are registered in (tools/list), and each module's import-time code uses only
earlier ones (later ones are imported at the bottom of a module, after its body).
"""

# isort: off
from . import (  # core first: its import loads the others in this order
    core,
    reporting,
    plumbing,
    overview,
    observe,
    world_tools,
    travel,
    dialogue,
    combat,
    memory,
    inventory,
    progression,
    uitools,
    newgame,
    batch,
    queueing,
    turn_plan,
    turn_slots,
    turn_tool,
    macro_tools,
    travel_macro,
    saving,
    session,
)
# isort: on

MODULES = (
    core,
    reporting,
    plumbing,
    overview,
    observe,
    world_tools,
    travel,
    dialogue,
    combat,
    memory,
    inventory,
    progression,
    uitools,
    newgame,
    batch,
    queueing,
    turn_plan,
    turn_slots,
    turn_tool,
    macro_tools,
    travel_macro,
    saving,
    session,
)
