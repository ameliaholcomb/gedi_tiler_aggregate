"""Run a planned dataset's unfinished partitions as DPS jobs.

Each submission splits the unfinished partitions among n jobs and writes
the split to <dataset>/_jobs/<tag>.json; job i computes the i-th share.
Submit again once the jobs end to retry the partitions that failed: it
only ever submits unfinished ones.
"""

import time

from gtagg import store

ALGO_ID = "gedi-tiler-aggregate"


def submit(dataset, n_jobs, algo_version, queue="maap-dps-worker-8gb",
           algo_id=ALGO_ID, burst=50, pause=180):
    from maap.maap import MAAP

    plan = store.read_json(store.join(dataset, "_plan.json"))
    todo = sorted(set(plan) - store.done_partitions(dataset))
    if not todo:
        print("All partitions are finished.")
        return
    n_jobs = min(n_jobs, len(todo))
    # Interleaved, so each job gets a mix of large and small partitions.
    shares = [todo[i::n_jobs] for i in range(n_jobs)]
    tag = time.strftime("%Y%m%d-%H%M%S")
    store.write_json(store.join(dataset, "_jobs", f"{tag}.json"), shares)
    print(f"Submitting {n_jobs} jobs for {len(todo)} partitions (tag {tag})")

    maap = MAAP(maap_host="api.maap-project.org")
    for i in range(n_jobs):
        for attempt in range(5):  # the MAAP API often times out
            try:
                job = maap.submitJob(
                    identifier=f"gtagg_{tag}_{i}", algo_id=algo_id, version=algo_version,
                    queue=queue, dataset=dataset, tag=tag, job=str(i),
                )
                if job.status != "failed":
                    break
                print(f"Job {i} failed to submit: {job.error_details}")
            except Exception as e:
                print(f"Job {i} failed to submit: {e}")
            time.sleep(30)
        else:
            raise SystemExit(f"Gave up submitting job {i}; submit again to retry.")
        # Bursts of DPS submissions can overload the MAAP API.
        if (i + 1) % burst == 0 and i + 1 < n_jobs:
            print(f"Submitted {i + 1}/{n_jobs}; pausing {pause} s")
            time.sleep(pause)
    print(f"Submitted {n_jobs} jobs. Check progress with `gtagg status {dataset}`.")


def job_partitions(dataset, tag, job):
    return store.read_json(store.join(dataset, "_jobs", f"{tag}.json"))[job]


def status(dataset):
    plan = store.read_json(store.join(dataset, "_plan.json"))
    done = store.done_partitions(dataset) & set(plan)
    print(f"{len(done)}/{len(plan)} partitions finished")
    return len(done), len(plan)
