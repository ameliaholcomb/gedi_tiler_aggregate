"""Compute a dataset's unfinished partitions as a batch of DPS jobs.

A submission splits the unfinished partitions among n jobs, all sharing one
DPS identifier, <batch>_<generation>, which names the batch's page on the
MAAP console. The split is saved to <dataset>/_jobs/<batch>_<generation>.json,
and job i computes its i-th share. Rerunning gt_aggregate / gt_from_img once
a batch ends submits a new generation for any partitions still unfinished.
"""

import os
import time

from gtiler.common.s3_utils import call_maap_api

from gtagg import store

ALGO_ID = "gedi-tiler-aggregate"
CONSOLE = "https://console.maap-project.org/batches"


def _generation(dataset, batch):
    fs, p = store.fs_path(store.join(dataset, "_jobs"))
    if not fs.exists(p):
        return 1
    prefix = f"{batch}_"
    gens = [os.path.basename(f).removeprefix(prefix).removesuffix(".json")
            for f in fs.ls(p, refresh=True) if os.path.basename(f).startswith(prefix)]
    return max([int(g) for g in gens if g.isdigit()], default=0) + 1


def submit(dataset, todo, n_jobs, batch, algo_version,
           queue="maap-dps-worker-8gb", burst=50, pause=180):
    from maap.maap import MAAP

    if not dataset.startswith("s3://"):
        raise SystemExit("DPS jobs need the dataset on S3 (-o s3://...)")
    batch = batch or f"gt_{os.path.basename(dataset)}"
    tag = f"{batch}_{_generation(dataset, batch)}"
    n_jobs = min(n_jobs, len(todo))
    # Interleaved, so each job gets a mix of large and small partitions.
    shares = [todo[i::n_jobs] for i in range(n_jobs)]
    store.write_json(store.join(dataset, "_jobs", f"{tag}.json"), shares)
    print(f"Submitting {n_jobs} jobs for {len(todo)} partitions as batch {tag}")

    maap = call_maap_api(MAAP, maap_host="api.maap-project.org")
    for i in range(n_jobs):
        job = call_maap_api(
            maap.submitJob, identifier=tag, algo_id=ALGO_ID, version=algo_version,
            queue=queue, dataset=dataset, tag=tag, job=str(i),
        )
        if job.status == "failed":
            raise SystemExit(f"Job {i} failed to submit: {job.error_details}\n"
                             "Rerun the same command to submit the rest.")
        # Bursts of submissions can overload the MAAP API.
        if (i + 1) % burst == 0 and i + 1 < n_jobs:
            print(f"Submitted {i + 1}/{n_jobs}; pausing {pause} s")
            time.sleep(pause)
    print(f"Submitted {n_jobs} jobs. Follow them at {CONSOLE}/{tag}\n"
          "When they end, rerun the same command: it resubmits any unfinished "
          "partitions, or says that all are finished.")


def job_partitions(dataset, tag, job):
    return store.read_json(store.join(dataset, "_jobs", f"{tag}.json"))[job]
