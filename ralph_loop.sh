#!/bin/bash
set -e

MAX=${1:-5}

echo "Starting Ralph - Max $MAX iterations"
echo ""

for ((i=1; i<=MAX; i++)); do
  echo "========================================"
  echo "  Iteration $i of $MAX"
  echo "========================================"
  echo ""

  result=$(claude --dangerously-skip-permissions \
    --output-format text \
    -p @PRD.md @progress.txt <<'EOF'
    You are Ralph, an autonomous coding agent.
    Do exactly ONE task per iteration.

    ## Steps

    1. Read PRD.md and find the first task that is NOT complete (marked [ ]).
    2. Read progress.txt - check the Learnings section first for patterns from previous iterations.
    3. Implement that ONE task only.
    4. Run tests/typecheck to verify it works.

    ## Critical: Only Complete If Tests Pass

    If tests PASS:
    - Update PRD.md to mark the task complete (change [ ] to [x])
    - Commit your changes with message: feat: [task description]
    - Append what worked to progress.txt

    If tests FAIL:
    - Fix the errors.
    - Do NOT mark task complete.
    - Do NOT commit.
    - Append what failed to progress.txt.

    Important:
    - Never do more than one task.
    - Never skip tests.
    - Keep changes minimal and focused.
EOF
)

  echo "$result"
  echo ""

  echo "Sleeping 2s before next iteration..."
  sleep 2
done

echo ""
echo "Ralph finished $MAX iterations."