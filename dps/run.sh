#!/usr/bin/env -S bash --login
set -euo pipefail
# Called by DPS for each job: compute one share of a submission's partitions.
# Inputs, in the order of the algorithm config:
#   $1 dataset  the dataset directory on S3 (planned by gt_aggregate / gt_from_img)
#   $2 tag      the submission's tag (its partitions are in <dataset>/_jobs/<tag>.json)
#   $3 job      this job's index in that submission
# Results go straight to the dataset on S3; DPS keeps only the logs.

mkdir -p output
conda run --live-stream -n gtagg gt_run "$1" --tag "$2" --job "$3"
