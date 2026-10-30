#!/usr/bin/env bash
# Launch the experiment demo. run_experiment_demo.py is the orchestrator:
# it boots the workstation, then spawns the Vite frontend (port 3471) as a
# child and tears it down on exit. Pass the *scaffolded* runtime workdir repo
# (create one once with `uv run python scaffold_workdir.py <dest>`) — the
# workflow's durable state lives under its data/ dir, so relaunching against
# the same workdir is the resume arc. Any extra args forward to
# run_experiment_demo.py (e.g. --enable-sandbox, --no-frontend).
#
#   uv run python scaffold_workdir.py /path/to/runtime_workdir   # once
#   ./start_demo.sh /path/to/runtime_workdir

set -euo pipefail
cd "$(dirname "$0")"

if [ "$#" -lt 1 ]; then
	echo "usage: $0 <scaffolded-workdir> [run_experiment_demo.py args...]" >&2
	echo "  (scaffold one first: uv run python scaffold_workdir.py <dest>)" >&2
	echo "  (or run: uv run python run_experiment_demo.py --auto-workdir)" >&2
	exit 2
fi

workdir="$1"
shift
exec uv run python run_experiment_demo.py --workdir "$workdir" "$@"
