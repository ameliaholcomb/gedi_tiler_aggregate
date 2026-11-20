#!/usr/bin/env -S bash --login
set -euo pipefail
# Called by DPS to build the algorithm's environment.
basedir=$(dirname "$(readlink -f "$0")")

conda env create -y -f ${basedir}/../environment.yml
conda run -n gtagg python -m pip install -e ${basedir}/..
# Install DuckDB's community h3 extension now, not in every job.
conda run -n gtagg python -c "import duckdb; duckdb.connect().install_extension('h3', repository='community')"
