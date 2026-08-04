#!/usr/bin/env bash
# Extracted from .github/workflows/watch.yml on 2026-08-04 so that TWO scheduled
# workflows (watch.yml, watch_b.yml) can share ONE copy of the loop. Both use the
# same `sentinel` concurrency group, so only one can ever run and there is no risk
# of double-sampling; the second schedule exists only to raise the chance that a
# `*/5` tick is honoured soon after a run ends. Do NOT duplicate this logic into a
# workflow file again -- two divergent copies of a loop this fiddly is how the
# `set -e` bug survived seven failures.
#
# Invoked as `bash scripts/sample_loop.sh` -- note NOT `bash -e`, which matters:
# the `set +e` below is defensive, but the caller must not re-arm errexit.

# DELIBERATELY NOT `set -e`. GitHub runs `run:` blocks as `bash -e {0}`,
# and that killed this job 7 times between 07-30 and 08-01 (runs #229,
# #231, #234, #241, #245, #249, #254). Root cause: the last statement of
# the loop body used to be `[ $SPENT -lt $CYCLE ] && sleep ...`. When a
# cycle overran 300s the test returned 1, the AND-list returned 1, it was
# the last command in the body, and `set -e` exited the step. Every one of
# those 7 deaths landed 5.2-7.8 min after a sample written on a perfect
# 5.0-min cadence -- i.e. always mid-cycle, always just over budget. Each
# cost 10-98 min of blind coverage, because an unplanned death leaves no
# queued run to take over and the next honoured `*/5` tick is ~1h away.
set +e
set -u
git config user.name  "sentinel-bot"
git config user.email "sentinel@users.noreply.github.com"

# SECOND-ORDER BUG, found 2026-08-02: removing `set -e` above stopped the
# job dying, but the crash had been doing useful work -- it was the only
# thing that got a wedged job out of the way so a fresh one could take
# over. Run #264, the first to carry the set+e fix, ran its full 335-min
# budget from 03:44Z to 09:19Z and committed NOTHING: 5.67 hours dark,
# worse than any failure it replaced, and it exited "success" so nothing
# flagged it. A silent hang is strictly worse than a loud crash.
# So the loop now watches itself: if it stops making progress it exits 0
# on purpose, handing off to the queued run. Exit 0 (not a crash) so the
# handoff does not pollute the failure statistics.
STALL=0                    # consecutive cycles that landed nothing on the remote
STALL_MAX=3                # ~15 min of no progress is a wedge, not a blip

START=$(date +%s)
BUDGET=$((335 * 60))       # leave ~15 min of headroom under the 350 timeout
CYCLE=300                  # 5 minutes

while :; do
  NOW=$(date +%s)
  if [ $((NOW - START)) -ge $BUDGET ]; then
    echo "budget spent, exiting for the next run"
    break
  fi

  # A failing watcher must not kill the loop -- one bad upstream response
  # should cost one sample, not 5.6 hours of coverage.
  python3 watcher.py || echo "watcher.py exited $? -- continuing"

  # Belt and braces. watcher.py also invokes the probe (so it can start
  # mid-run, since steps are frozen at job start and this job loops for
  # 5.6h), but that path is not firing for reasons I could not diagnose
  # without Actions logs. This step is the guaranteed path from the next
  # job onwards. Double invocation is harmless: the probe is resumable
  # and becomes a no-op once the cohort is exhausted.
  python3 survivor_probe.py || echo "survivor_probe.py exited $? -- continuing"

  # Clear any state a previous cycle's interrupted rebase/merge left behind.
  # A half-finished rebase makes every later commit and push fail forever,
  # which is the most likely way #264 wedged itself.
  git rebase --abort  >/dev/null 2>&1
  git merge  --abort  >/dev/null 2>&1
  rm -f .git/index.lock

  # Pick up code pushed since this job started, so the next such fix
  # does not have to be smuggled in through watcher.py.
  git pull --rebase -q || echo "pull failed -- continuing on local copy"

  git add data/ || echo "git add failed -- continuing"
  if git diff --cached --quiet; then
    echo "no data change"
    STALL=$((STALL + 1))
  else
    if git commit -q -m "sample $(date -u +%Y-%m-%dT%H:%M)"; then
      COMMITTED=1
    else
      COMMITTED=0
      echo "commit failed -- continuing"
    fi
    # Three tries, then give up until the next cycle. Losing the push race
    # to an external PAT push is normal and must never end the job -- the
    # commit is local and the next cycle will carry it.
    PUSHED=0
    for i in 1 2 3; do
      if git push -q; then PUSHED=1; break; fi
      git pull --rebase -q || echo "rebase during push retry failed"
    done
    if [ "$PUSHED" = "1" ]; then
      STALL=0
    else
      STALL=$((STALL + 1))
      echo "push did not land this cycle (commit=$COMMITTED, stall=$STALL)"
      git status --short --branch 2>&1 | head -20
    fi
  fi

  # The self-monitor. Nothing has reached the remote for STALL_MAX cycles,
  # so this job is not doing its job. Stand down and let the next one try
  # with a clean checkout, rather than burning the rest of the budget dark.
  if [ "$STALL" -ge "$STALL_MAX" ]; then
    echo "STALLED: $STALL cycles with nothing landed on the remote."
    echo "Exiting 0 at $(date -u +%H:%M:%S) so the queued run takes over."
    break
  fi

  # sleep to the next cycle boundary, not a flat 300s, so drift does not
  # accumulate over 68 iterations. An overrunning cycle simply skips the
  # sleep -- it must NOT be expressible as a failing command.
  SPENT=$(( $(date +%s) - NOW ))
  if [ $SPENT -lt $CYCLE ]; then
    sleep $((CYCLE - SPENT))
  else
    echo "cycle overran ($SPENT s) -- skipping sleep, continuing"
  fi
done
echo "loop finished cleanly"
