## Two-team match
This session is one turn-based fight between two teams, side A and side B, each played by its own player and bg3
server. Your server plays one side: its characters are the party in every result (`get_status`, `get_party`). The
other team's characters are hostile combatants, listed in `get_combat_state` only while any of your characters sees
them; what you learn of them (events, positions, HP) is what your characters perceive. The other player's calls and
text never reach you, only their effects do.

- A side acts only when one of its characters is acting. An action at another time is refused at once ("side B is
  acting") and does nothing. `end_turn` and `wait_for` with `my_turn` wait until one of your characters is acting.
- In a turn group shared by both sides, the sides alternate one character at a time in the rolled order (opener's
  first, other's first, opener's second, ...); once a side has nobody left to act there, the other side's characters
  act on in order. {turn_order}
- `set_reactions` selects the character first, so it is refused like any action while the other side is acting.
- Actions with the other team's characters and polling or cancelling its jobs are refused ("<name> is not on your
  side", "not your job").
- Both sides share one game screen. The other side's reaction prompt is theirs to answer: while it is open your
  results show WAITING and the game waits; answering it is refused ("not your reaction").
- The match ends at a turn boundary when one side's characters are all downed or dead: the other side wins. At the
  round cap it is a draw. A turn gets {turn_cap_s} s of model time per character in it (the model's own time, not the
  tools'); past that it is ended for that side, and three such turns in a row lose the match.
- Durations count down at each round's end, for everyone: an N-turn status lasts the rest of its round plus N-1
  rounds. A prone character can't act or move; it stands up at its turn start for half its movement, if it has that
  much.
- A reaction prompt with a row per slot level (e.g. Shield) needs a click, which a match never makes: `react` with
  use=true fails on it; declining works; other prompts work as usual.
- Spells cast through a second pick (Dimension Door's landing, Revivify) are refused, and a cast takes at most two
  targets.
- A match removes these tools, which the rules above may name: the memory and note tools, the wiki, saves and reloads,
  screenshots, raw input, `ui_action`, `fast_travel`, `camp`, `swap_party`, `long_rest`, `short_rest`,
  `rest_if_needed`, `travel_to`, `talk_to`, `choose_dialogue`, `dialogue_advance`, `get_dialogue`, `roll_check`,
  `trade`, `pickpocket`, `lockpick`, `disarm`, `loot`, `loot_corpse`, `loot_area`, `pick_up`, `feed_gale`,
  `insert_item`, `read_all`, `place_marker`, `delete_marker`, `new_game`, `character_creation_options`,
  `create_character`, `get_level_up_options`, `level_up`, `prepare_spells`, `get_journal`, `get_map`.
