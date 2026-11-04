## Two-team match
This session is one turn-based fight in one game between two teams, side A and side B, each played by a different
player through its own bg3 server. Your server plays one side: its characters are the party in every result
(`get_status`, `get_party`). The other team's characters are hostile combatants, listed in `get_combat_state` only
while one of your characters can see them; what you learn of them (events, positions, HP) is what your characters
perceive. The other player's calls and text never reach you, only their effects in the game.

- A side acts only when one of its characters is acting. An action at another time is refused at once, naming the
  side that is acting ("side B is acting"), and nothing is done. `end_turn` and `wait_for` with `my_turn` wait until
  one of your characters is acting.
- Characters of both sides can share one turn group. Inside it the sides alternate one character at a time: the
  round's opening side's first character in the rolled initiative order, then the other side's first, then the
  opening side's second, and so on. A side with nobody left to act in the group: the other side's characters act on
  in order. {turn_order}
- `set_reactions` selects the character first, so it works only while one of your characters is acting; at another
  time it is refused: "<name> could not be selected: not your turn (side B is acting)".
- An action with a character of the other team is refused: "<name> is not on your side". A job of the other side
  can't be polled or cancelled: "not your job".
- Both sides share one game screen. A reaction prompt of the other side's character is that side's to answer: while
  it is open your results show WAITING and the game waits for that side; answering it is refused: "not your
  reaction".
- The match ends at a turn boundary when every character of one side is downed or dead: the other side wins. At the
  match's round cap it is a draw. A turn gets {turn_cap_s} s of model time per character in it (the model's own time, not
  the tools'); past that it is ended for that side, and three such turns in a row lose the match.
- A reaction prompt with one row per spell level (one reacting character whose reaction spell it can cast at more
  than one slot level, e.g. Shield) is answered only by a click on a row, and a match makes no clicks: `react` with
  use=true fails on it; declining it works. Prompts with one row (an opportunity attack, a reaction with one usable
  level) or with several reacting characters are answered as usual.
- Spells cast through a second pick (Dimension Door's landing, the Revivify spell) are refused, and a cast takes at
  most two targets.
- This match removes these tools, though the rules above may name them: the memory and note tools, the wiki, saves
  and reloads, screenshots, raw input, `ui_action`, `fast_travel`, `camp`, `swap_party`, `long_rest`, `short_rest`,
  `rest_if_needed`, `travel_to`, `talk_to`, `choose_dialogue`, `dialogue_advance`, `get_dialogue`, `roll_check`,
  `trade`, `pickpocket`, `lockpick`, `disarm`, `loot`, `loot_corpse`, `loot_area`, `pick_up`, `feed_gale`,
  `insert_item`, `read_all`, `place_marker`, `delete_marker`, `new_game`, `character_creation_options`,
  `create_character`, `get_level_up_options`, `level_up`, `prepare_spells`, `get_journal`, `get_map`.
