# Mailbox

`~/.gantry/current/kit/gantry-mailbox`, installed with the kit from `template/kit/gantry-mailbox`. The coordinator runs `wait` in the background. A daemon holds the Run's one unfiltered `orca orchestration check --wait`, journals each batch, and the coordinator reads the journal instead of calling `check`.

## Sub-features

- `wait [--types t1,t2] [--timeout-ms N]` starts the daemon if needed, then blocks until a journaled message matches the types, or the timeout passes. Default types are everything except `heartbeat` and `status`. Default timeout is 180000 ms.
- The daemon is the internal `daemon <run>` process. It loops `check --wait`, acks the previous batch, and appends to `$TMPDIR/gantry-mailbox/<run>/journal.jsonl`.
- `wait` exits with an error when the terminal has no bound Run.
- The guard allows the coordinator to run the mailbox and denies the coordinator `orca orchestration check`.

## How to get to it (user POV)

The operator does not run it. The coordinator, started with `gantry` on the main checkout, runs one background `gantry wait` per expected event. That command starts this daemon and blocks until a heartbeat has failed or a message needs the coordinator.

## Driving it with sandbox.sh

Tier 1 proves install and the guard. It does not start the daemon. The daemon needs a live Orca Run.

```sh
cmp -s <repo checkout>/template/kit/gantry-mailbox "$V/sbx/home/.gantry/current/kit/gantry-mailbox" && echo mailbox-match
test -x "$V/sbx/home/.gantry/current/kit/gantry-mailbox" && echo mailbox-exec
$S guard "$V" coordinator repo Bash "{\"command\":\"$V/sbx/home/.gantry/current/kit/gantry-mailbox wait --timeout-ms 180000\"}"
# ALLOW
$S guard "$V" coordinator repo Bash '{"command":"orca orchestration check --wait --timeout-ms 180000"}'
# DENY
```

`$S doctor "$V"` already requires the mailbox to match the template.

## Gotchas

- The path is the scratch home inside a sandbox. On a real machine it is `~/.gantry/current/kit/gantry-mailbox`.
- The basename `gantry-mailbox` is what the guard allows. A coordinator command that is not `orca`, `gantry overlap`, or that basename is denied.
