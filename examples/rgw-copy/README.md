# RGW copy helpers

Used in the RGW rebuild window ([docs/rgw-rebuild.md](../../docs/rgw-rebuild.md)) to move users, buckets and objects from a realm-less cephadm zone into a new Rook object store. Generalized from the scripts we ran; adjust zone selectors, bucket lists and owners.

| File | Where it runs | What it does |
|---|---|---|
| `rgw-snap.py` | Rook toolbox | Sanitized snapshot of one zone (users with key fingerprints only, buckets, flags, ACLs, lifecycle, counts) |
| `rgw-diff.py` | workstation | Compares two snapshots: users, keys (by fingerprint), quotas, caps, flags, owners, ACLs, lifecycle |
| `copy-users.sh` | Rook toolbox | `metadata get` piped into `metadata put` per user; verifies each user resolves by access key in the new zone |
| `copy-job.yml` | Kubernetes Job | Creates buckets as their owners, copies current versions with rclone, gates count/bytes/check/sample per bucket |

Order in the window: snapshot old -> freeze and prove zero writes ([docs/flux-freeze.md](../../docs/flux-freeze.md)) -> snapshot old again -> copy users -> copy job -> snapshot new and old -> diff (old vs new, and old-before vs old-after must be identical) -> stop the legacy RGW -> cut clients over.

Rules we kept:

- Keys never appear on a terminal or in a file you keep. They move through pipes, Kubernetes Secrets consumed as env, or 0600 temp files that are deleted.
- The snapshot of the source is taken **after** the zero-write watch. If old-before and old-after differ, a writer got through: re-freeze, re-watch, re-snapshot and run a delta pass (`SYNC_BUCKETS` for buckets with deletes).
- "hashes could not be checked" from `rclone check` means multipart objects without an MD5 on one side; they are size-checked, and the 100-object download sample covers content.
