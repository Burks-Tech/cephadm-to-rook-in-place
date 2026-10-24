# RGW: rebuild, do not adopt

The cephadm RGW (realm-less zone `default`, three daemons behind a haproxy/keepalived VIP) was replaced by a new Rook `CephObjectStore` and every client was cut over in one evening window. [PROD]. Users and their access keys, buckets, versioning, object lock, lifecycle rules and current object versions were copied; the legacy zone and its pools were left untouched as the fallback.

Examples: [examples/objectstore/](../examples/objectstore/), copy helpers in [examples/rgw-copy/](../examples/rgw-copy/).

## Starting state

- Zone and zonegroup `default`, **no realm**, pools `default.rgw.*` (8 pools, 768 PGs, three placement targets).
- 12 users, 12 buckets, about 270 GiB in the buckets that mattered.
- Clients: Velero, CloudNativePG WAL archiving and base backups, a container registry's package storage, a few apps, rclone and restic jobs that ship backups offsite, and a guest-backup forwarder VM.
- By the time of the window only one cephadm RGW was left (the others had been on converted hosts), behind the ingress VIP on the last unconverted host.

## Why rebuild instead of adopting the realm-less zone

From Rook v1.21.0 source [SRC]:

- Rook always starts radosgw with `--rgw-realm=R --rgw-zonegroup=ZG --rgw-zone=Z` and names all three after the store. A store named `default` would create an empty, detached realm `default`, find the zonegroup and zone by name, and stage a period that stays empty (`update_period` skips zonegroups with a different `realm_id`). RGW would start against a realm whose period has no zonegroup: unproven.
- Deleting such a store later runs `zone delete`, `zonegroup delete` and `realm rm` against live legacy metadata.
- Adopting through `CephObjectZone` CRs first needs a hand-made realm, `zone modify`, `period update --commit`, pre-creating missing pools (our legacy zone referenced two pools that did not exist), and deleting a Zone CR destroys the zone. `sharedPools` rewrites placement references and deletes unlisted placements.
- At pool level only `CephObjectStore.metadataPool` can adopt `default.rgw.*` pools, and Rook hard-sets `pg_num_min 8` on them, so live 32-PG pools could be merged by the autoscaler.
- The lab analysis rated adoption unsafe, and our policy is "correct end state over zero-downtime workarounds".

So: build a new single-site store (realm = zonegroup = zone = `s3`, its own pools `s3.rgw.*`), copy users, buckets and objects into it in one offline window, leave the legacy zone and pools untouched as the fallback until a soak passes, then delete them in a separate step. If your cluster already has a real realm and zones, adoption through `CephObjectZone` may be a different calculation; we did not test it.

## The store as built

```yaml
apiVersion: ceph.rook.io/v1
kind: CephObjectStore
metadata: {name: s3, namespace: rook-ceph, annotations: {kustomize.toolkit.fluxcd.io/prune: disabled}}
spec:
  preservePoolsOnDelete: true       # deleting a single-site store runs zone/zonegroup/realm delete for s3
  defaultRealm: false               # a default realm would change how the legacy zone resolves while it is the fallback
  metadataPool: {failureDomain: host, deviceClass: ssd, replicated: {size: 3}, parameters: {pg_num: "8"}}
  dataPool:     {failureDomain: host, deviceClass: hdd, replicated: {size: 3}, parameters: {pg_num: "64"}}
  gateway:
    port: 8081                      # NOT 8080 (mgr dashboard on mgr nodes) and NOT 7480 (legacy haproxy backends)
    securePort: 8444                # NOT 8443 (dashboard ssl_server_port)
    sslCertificateRef: <tls-secret> # kubernetes.io/tls Secret; must exist BEFORE the CR
    instances: 3
    hostNetwork: true               # one pod per node
    resources: {requests: {cpu: "500m", memory: "1Gi"}, limits: {memory: "4Gi"}}   # legacy RGW RSS was about 610 MiB
    placement: <storage-node affinity and toleration>
  hosting:
    advertiseEndpoint: {dnsName: <lb-service>.rook-ceph.svc, port: 80, useTls: false}
    dnsNames: [s3.example.com, <lb-service>.rook-ceph.svc.cluster.local, <guest-forwarder-name>]
  healthCheck:
    startupProbe:   {probe: {initialDelaySeconds: 10, timeoutSeconds: 10, periodSeconds: 10, failureThreshold: 60}}
    readinessProbe: {probe: {initialDelaySeconds: 10, timeoutSeconds: 10, periodSeconds: 10, failureThreshold: 6}}
```

Notes:

- **Ports.** With `hostNetwork: true`, the RGW shares the node's ports with the mgr. The mgr dashboard listened on `:::8080` on both mgr nodes and its `ssl_server_port` was 8443, so an RGW pod scheduled on a mgr node could not bind 8080/8443. 7480 was out because the legacy haproxy still listed old-zone backends on it; a new RGW there would have answered 404 for every old bucket. Clients never see these ports: the Service maps 80 to 8081 and 443 to 8444.
- **First reconcile** created the realm, zonegroup and zone (no `--default`), the `s3.rgw.*` pools with pool-named, class-aware CRUSH rules, and added objects to `.rgw.root`. The legacy zone's objects were not changed (verified by hash). It also changed `.rgw.root` itself: see deviation 1 below.
- **`pg_num`** is set explicitly because the cluster ran with the autoscaler off and OSDs already held up to about 205 PGs; check `mon_max_pg_per_osd` before adding pools.
- **`hosting.dnsNames`.** Rook writes `rgw dns name` from `advertiseEndpoint` plus this list plus its own Service name, and the Rook docs warn that RGW then rejects Host headers not on the list. We listed every Host a client sends (path-style clients) and smoke-tested each, including the raw LB IP.
- **`advertiseEndpoint`** is what the operator itself uses for admin ops and health checks. We pointed it at the in-cluster LoadBalancer Service over plain HTTP, so Rook's reconcile depends on nothing outside the cluster (no DNS record, no proxy) and needs no certificate check; a public certificate cannot carry a `*.svc` name.
- An inherited setting: a legacy `client.rgw rgw_sigv4_insecure=true` row (an old proxy workaround) is inherited by prefix by every `client.rgw.*` daemon, including Rook's. Check with `ceph config show`.

## Endpoint design as built

- **One LoadBalancer Service** in front of the three RGW pods (`http` 80 to 8081, `https` 443 to 8444), with its own static IP. Rook's own RGW Service is headless with hostNetwork, so clients use this one. `externalTrafficPolicy: Cluster`, because the MetalLB speakers run only on the ordinary Kubernetes nodes, not on the tainted storage nodes.
- **Announce the LB IP only from fast nodes, on the Kubernetes VLAN interface.** With MetalLB's default L2 advertisement the IP was answered by a 1G control-plane node and LAN S3 capped at 1 Gbit. We added a dedicated advertisement limited to 10G/25G nodes and pinned to their VLAN interface ([after-the-migration.md](after-the-migration.md#s3-loadbalancer-announce-only-from-fast-nodes-on-the-right-interface)).
- **In-cluster clients:** `http://<lb-service>.rook-ceph.svc.cluster.local`.
- **LAN clients:** `https://s3.example.com`, resolving to the LB IP (split-horizon DNS); RGW terminates TLS itself with a cert-manager certificate. No reverse proxy in the path.
- **Off-LAN clients:** a CDN tunnel whose origin is the in-cluster Service on port 80 (Host header kept).

The original plan was TLS passthrough through the cluster's Traefik gateway (needed because Traefik, like any Go reverse proxy, drops `Content-Length: 0` on bodyless requests and breaks AWS CRT signatures, [P-11](pitfalls.md#p-11-aws-crt-client-fails-behind-a-tls-terminating-proxy)). Whether Traefik would program a passthrough TLSRoute on the same port as an existing HTTPS listener was an open question, so we took the fallback: a dedicated LB IP with RGW terminating TLS. With it, the aws-cli v2 default CRT client passed the smoke test.

RGW reads its certificate only at start. Plan a restart (or an automated restarter) before the certificate expires; Rook does not roll the RGW Deployments when the Secret changes.

## Transport test before the window [PROD]

Throwaway user and bucket, pinned tool versions, 1 GiB object, path-style, sha256 compared. Through Traefik with TLS termination: aws-cli v2 with `preferred_transfer_client = classic` PASS (put about 170 MiB/s, get about 128); rclone PASS (put about 51, get about 68); restic PASS (about 54 and 79). aws-cli v2 default CRT client: listing, uploads and all `s3api` calls work; every `aws s3 cp` download and `aws s3 rm` fails (`SignatureDoesNotMatch`). Through the CDN tunnel (about 2 MB/s uplink) large multipart uploads hit the CDN's 100-second origin timeout.

## The window

Booked 4 hours, estimated 2.5 to 3.5 hours, took 3 h 17 min including about two hours of recovery from the freeze failure described below.

| Step | What | Gate |
|---|---|---|
| W0 | Baseline: health, PGs, sanitized snapshot of the legacy zone (users with key fingerprints only, buckets, flags, ACLs, lifecycle, counts), pool and rule dumps, zone and zonegroup hashes, realm list | all present |
| W1 | Merge the store commit: certificate, LB Service, then the CephObjectStore | store Ready, 3 RGW pods 0 restarts; **G-LEGACY**: legacy zone and zonegroup hashes identical; **POOLRULE**: no existing pool changed (failed, deviation 1); smoke test on every endpoint and Host header (100 MiB multipart put/get/head/list/delete, throwaway user purged) |
| W2 | Freeze every writer ([flux-freeze.md](flux-freeze.md)) and remove the in-cluster path to the legacy RGW | zero-write watch on the legacy RGW request log for 3+ minutes |
| W3 | Copy users: `radosgw-admin metadata get user:<uid>` piped straight into `metadata put` in the new zone (same uid, display name, keys, quotas, caps, flags); never displayed or written to disk | each copied user resolves **by access key** in the new zone |
| W4 | Create buckets **as their owners** (each owner's own key), versioning, object lock, lifecycle | idempotent init container |
| W5 | Copy current object versions with rclone (`-M` for metadata), owner keys on both sides, four parallel streams | **G-DATA** per bucket: count and bytes equal (`rclone size`), `rclone check` 0 differences, a 100-object full download-compare sample |
| W5b | Snapshot the new zone and diff against the legacy snapshot | users, keys (fingerprints), quotas, caps, flags, owners, ACLs, lifecycle all equal (239 checks); **G-LEGACY** again: legacy snapshot after the copy equals the one before |
| W6 | Stop the legacy RGW unit | its port refuses connections |
| W7 | Client cutover commit set: Velero BSL, 12 CloudNativePG object stores, registry storage, apps, offsite jobs, DNS record, CDN tunnel origin, guest forwarder, firewall rule for the guest VM, monitoring probes | one A record; every client points at the new endpoint |
| W8 | Unfreeze | nothing left suspended |
| W9 | Client gates: CNPG archiving and an on-demand base backup; Velero backup and restore to a scratch namespace; registry pull plus a CI build that pushes; app reads; offsite jobs; guest forwarder checks; zero `SignatureDoesNotMatch` in 30 minutes | all PASS |
| W10 | Stop haproxy and keepalived; the ingress VIP is gone | |

Why each bucket is created and written with its owner's own key: a migration or admin user would become the owner of every copied object, and the bucket owner could then be denied access to them.

Copy time: the first pass took 911 seconds for about 285 GB across 9 buckets; the final delta pass took 9 seconds. Not copied, by decision: two buckets of an app being retired and one empty bucket, plus cephadm's own dashboard user.

## Deviations and findings

1. **Rook rewrote `.rgw.root` pg_num 32 to 8.** Rook applies the store's `metadataPool` spec, plus a hardcoded `pg_num` / `pg_num_min` of 8 (`rgwRadosPoolPgNum` in `pkg/operator/ceph/object/objectstore.go`), to the shared `.rgw.root` pool on every reconcile. That pool holds the zone, zonegroup and realm objects of both zones. It went 32 to 8 PGs (the merge took about 30 minutes; crush rule and size unchanged). The POOLRULE gate caught it; we held, did not set it back, and accepted it as a Rook-managed difference. Next time expect it: Rook would re-apply it anyway.
2. **The GitOps controller undid the freeze, twice, and one writer was not on the list.** The first zero-write watch passed only because it ran before the next 30-minute reconcile. The reconcile restored replicas, CronJob and ScheduledBackup `suspend`, and re-created the in-cluster Endpoints to the legacy RGW through a parent Kustomization; the legacy zone took writes again (database WAL archives and app objects). The W5b G-LEGACY comparison caught it. The first recovery suspended the owning Kustomizations but not their parent, which re-applied the child with `suspend: false` eight minutes later. That second leak also revealed Velero's kopia repository maintenance Jobs as a writer nobody had listed. The second recovery suspended the parent too, scaled Velero to 0 and suspended its HelmRelease; then a zero-write watch, a fresh snapshot and a delta copy (`rclone sync` for the bucket with deletes, copy for the rest). Cost: about two hours. Details and the checklist: [flux-freeze.md](flux-freeze.md).
3. **The delta job's init failed on suspended users' buckets.** Two users were suspended in the source (and re-suspended after the first copy); `head-bucket` with their keys fails, so the init tried to create the buckets. Fix: drop those buckets from the delta (they had not changed).
4. **Stacked pull requests.** The client-cutover change was stacked on the store change; after the first was rebase-merged, the second stopped being mergeable. Rebase it onto the merged branch (git skips the applied patch) and check with `git range-diff` before the window, not in it.
5. **Firewall policy order.** A rule allowing the guest VM to reach the new LB IP was appended below existing block rules by the firewall's API (it ignored the requested index), so it was shadowed. Check rule order, not just rule existence.
6. **A secret printed by a status command.** `ceph config dump | grep rgw` printed the dashboard's RGW access and secret keys into a terminal log. See the rotation note below and [P-18](pitfalls.md#p-18-secrets-leaking-into-tool-output).
7. **Mgr crash side effect.** The earlier `PROMETHEUS_API_HOST` change crashed the dashboard and prometheus modules on a cephadm standby mgr with a port clash ([P-20](pitfalls.md#p-20-mgr-dashboard-port-clashes)). The active Rook mgr was unaffected.

## Rotating the dashboard's RGW key under Rook

The dashboard talks to RGW with a system user's key stored in the mgr (`ceph dashboard get-rgw-api-access-key`). On every CephObjectStore reconcile Rook [SRC]:

1. reads the current mgr keys,
2. compares them with the first key of RGW user `dashboard-admin` in the store's realm,
3. on a match does nothing; on a mismatch or a missing user it creates (or deletes and re-creates) `dashboard-admin` **with the mgr's keys**, then writes them back to the mgr.

So the mgr setting is the source of truth and is copied into the user. That is why the key cephadm had set for its own `dashboard` user in the legacy zone appeared in the new zone too. Consequences: regenerating the key on the user alone is undone by Rook; resetting the mgr keys alone leaves the match check true and the dashboard then creates its own user on the next RGW page view. The correct rotation [PROD]:

```bash
R="--rgw-realm=s3 --rgw-zonegroup=s3 --rgw-zone=s3"
radosgw-admin key create --uid=dashboard-admin --key-type=s3 --gen-access-key --gen-secret $R   # write output to a 0600 temp file, never the terminal
ceph dashboard set-rgw-api-access-key -i <file-with-access-key>
ceph dashboard set-rgw-api-secret-key -i <file-with-secret-key>
radosgw-admin key rm --uid=dashboard-admin --access-key=<old-access-key> $R
# legacy zone: remove the old key from cephadm's `dashboard` user and suspend it
```

Verify: the old key gets `InvalidAccessKeyId`, the new one authenticates, the dashboard's RGW pages answer, and after the next Rook reconcile `dashboard-admin` was not re-created (its metadata mtime is unchanged).

## Legacy zone removal [PLANNED]

Earliest seven days after the old RGW was stopped, and only after: daily Velero backups completed every day; every CloudNativePG cluster archiving with at least one scheduled base backup on the new store; offsite jobs green; one Velero restore test and one database recovery test from the new store.

1. **Export** (to offline storage; the `meta` pool export contains user keys): `rados export` of `.rgw.root` and the small `default.rgw.*` pools, zone and zonegroup JSON, bucket and object listings. The large data pool is not exported (its buckets were verified src = dst).
2. **Remove the legacy zone and zonegroup** from `.rgw.root` (realm `s3` untouched): `radosgw-admin zonegroup remove --rgw-zonegroup=default --rgw-zone=default`, `zone delete --rgw-zone=default`, `zonegroup delete --rgw-zonegroup=default`. Gate: the hashes of every realm-`s3` object in `.rgw.root` unchanged, `zonegroup list` and `zone list` show only `s3`, smoke test on the new store. Never pass `--rgw-realm=s3` to these commands; no `period update --commit` (the legacy zonegroup is not in any period).
3. **Make realm `s3` the default** right after (Git: `defaultRealm: true`). Until then, always pass `--rgw-realm/--rgw-zonegroup/--rgw-zone` to `radosgw-admin`: with no default realm and no default zone left, a flagless call can auto-create a fresh `default` zone and `default.rgw.*` pools.
4. **Delete the legacy pools**, small ones first and the large data pool last, in a quiet period (PG removal loads the HDDs), under a short pool-scoped `mon_allow_pool_delete` window with `nodelete` lifted per pool. Then the legacy RGW cephx entities and config rows, and any CRUSH rule used only by the deleted pools (a CRUSH change: separate approval).
5. Firewall rules, IPS exceptions, DNS records and documentation that still name the legacy addresses.
