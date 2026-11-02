PACE PRE-FLIGHT (read before your first game action):
- The character must be busy while you think. A walk is: issue it, then do something that doesn't
  depend on it, then react to its result. For example:
    travel_to(objective="o1", wait=false)          -> started, returns at once
    queue_actions('[{"tool":"interact","target":"e15"}]')   -> what to do on arrival
    look() / get_frontier() / note("[tag] ...")      -> while it walks
    wait_for("queue")                              -> only now, if nothing else is left to do
- Pass wait=false to move_to, walk_toward and travel_to (they block by default), also after a walk
  stopped or failed (failures come back at once anyway). Never
  wait() to pass time: wait_for("my_turn" | "idle" | a job id).
- Stuck (a loop, a bugged action, a route that keeps failing) never ends the session: follow
  knowledge/playbooks/stuck.md section 0 (cancel and retry differently, reload the last good save, lead
  with another party member, another objective) and keep playing until the ⏱ line says WRAP UP NOW.
