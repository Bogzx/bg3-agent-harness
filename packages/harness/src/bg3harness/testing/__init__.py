"""Offline test doubles for the harness: a fake BG3AI mod the real bridge can talk to.

    fakemod.FakeMod      TCP server speaking the game's LuaDebug protocol; answers RPC evals from responders
    scripted.GameModel   scripted game state (party, gold, items, fight, dialogue, area, referee/roll/scenario rings)
    fixtures.FixtureStore  replays files recorded with BG3AI_RPC_RECORD (bridge/src/bg3ai/rpc_record.py)
    mcp_bridge           starts bg3ai-mcp against a fake mod and calls its tools as an MCP client

Recorded fixtures live in packages/harness/tests/fixtures/rpc/.
"""
