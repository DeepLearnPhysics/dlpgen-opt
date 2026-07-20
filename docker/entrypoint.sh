#!/bin/bash
set -euo pipefail

source /opt/geant4/bin/geant4.sh

if [[ $# -eq 0 ]]; then
    exec dlpgen-opt --help
fi

case "$1" in
    bash|sh|python|python3|edep-sim|dlpgen|run_edep2supera.py|dlpgen-opt|sim-production)
        exec "$@"
        ;;
    *)
        exec dlpgen-opt "$@"
        ;;
esac
