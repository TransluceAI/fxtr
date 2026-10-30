#!/usr/bin/env bash
# Launch the assembly-stage demo. run_demo_workstation.py is the orchestrator:
# it boots the workstation, then spawns the Vite frontend (port 3470) as a
# child and tears it down on exit. Pass the *scaffolded* runtime workdir repo
# (create one once with `uv run python scaffold_workdir.py <dest>`); any extra
# args forward to run_demo_workstation.py (e.g. --allow-llm-apis, --no-frontend).
#
#   uv run python scaffold_workdir.py /path/to/runtime_workdir   # once
#   ./start_demo.sh /path/to/runtime_workdir

set -euo pipefail
cd "$(dirname "$0")"

if [ "$#" -lt 1 ]; then
	echo "usage: $0 <scaffolded-workdir> [run_demo_workstation.py args...]" >&2
	echo "  (scaffold one first: uv run python scaffold_workdir.py <dest>)" >&2
	exit 2
fi

workdir="$1"
shift
exec uv run python run_demo_workstation.py --workdir "$workdir" "$@"
