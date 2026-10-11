---
name: resume-task
description: Resume a FlowForge pipeline task that failed because of a step timeout. Use when the user says "resume <task-id>", "restart f9-d1", "continue the timed-out task", or asks to retry a task killed by the step timeout. Use ONLY for timeout-killed tasks; real errors need user attention first.
---

# Resume a timed-out FlowForge task

A timed-out step marks the task `failed` but keeps all partial work: earlier
step outputs exist on disk and the opencode session id is recorded, so a rerun
continues the SAME session from the first missing output file. Do not reset
the task or delete anything.

## Steps

1. Confirm the task is a timeout case:

```bash
python3 -c "
import json
for line in open('tasks.jsonl'):
    t = json.loads(line)
    if t.get('id') == '<TASK_ID>':
        print(t.get('status'), t.get('last_run_log'))
"
```

   Status must be `failed`. Then check the log tail: `tail -5 <last_run_log>`
   (or the newest `runs/*_<TASK_ID>.log`). Look for "timed out". If the
   failure is a provider/model error instead, STOP and tell the user instead
   of resuming.

2. Make sure no other run is active (the runner takes a lock and would exit
   immediately if one is):

```bash
python3 watch-task.py --once
```

   If a task shows as running, do not launch; tell the user.

3. Launch the resume detached, with a one-off 2h step timeout (7200; the user
   may name another value or `0` for unlimited — pass it through):

```bash
nohup python3 run-task.py --task-id <TASK_ID> --step-timeout 7200 \
  >> runs/resume_<TASK_ID>.out 2>&1 &
```

4. Wait ~15s, then verify it picked the task up and resumed the execution
   step (status back to `running`, log growing):

```bash
python3 watch-task.py --once
```

5. Report to the user: which step resumed, the session id, the new log file,
   and that the timeout is now 2h. Offer to check progress later on request
   (`python3 watch-task.py --once`, or `tail` the run log — heartbeat lines
   appear when a step is silent).

## Notes

- Never edit `tasks.jsonl` by hand for a resume; `run-task.py` handles status.
- If the lock is held, another run is active — report and do nothing.
- Exit codes: 0 ok, 1 task failure, 2 config error (check
  `runs/resume_<TASK_ID>.out` on failure).
