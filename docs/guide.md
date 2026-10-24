# Migrating a cephadm Ceph cluster to Rook in place (zero data movement)

Nothing in this document is a recommendation from the Rook or Ceph projects. It is a write-up of one real production migration, plus the lab work that came before it. Each claim carries an evidence label (section 1.2). Parts that were only planned are labelled as such.

Worked example: Ceph 20.2.4 (Tentacle), 7 bare-metal hosts, 44 OSDs, 7 mons, 18 pools at the start, an erasure-coded 5+2 data pool on exactly 7 hosts, 2 CephFS filesystems, RGW, SMB. Moved from Ubuntu + cephadm (Docker) to Talos Linux + Rook v1.21.0, one host at a time, with the fsid, mon names and addresses, OSD ids, cephx keys, pools and CRUSH map unchanged. All seven hosts are converted, both filesystems run on Rook MDS daemons, S3 runs on a new Rook object store, SMB runs as a plain Samba pod and the cephadm module is disabled. What is still open is listed in [Appendix D](#appendix-d-evidence-status-and-open-items).

## Contents

1. [Read this first](#1-read-this-first)
2. [The idea in one page](#2-the-idea-in-one-page)
3. [Phase 0: feasibility and lab rehearsal](#3-phase-0-feasibility-and-lab-rehearsal)
4. [Phase 1: Rook alongside cephadm, nothing moves](#4-phase-1-rook-alongside-cephadm-nothing-moves)
5. [Phase 2: per-host conversion](#5-phase-2-per-host-conversion)
6. [Phase 3: service daemons](#6-phase-3-service-daemons)
7. [Phase 4: the last host and the cephadm decommission](#7-phase-4-the-last-host-and-the-cephadm-decommission)
8. [Gates and rollback, step by step](#8-gates-and-rollback-step-by-step)
9. [Pitfalls](#9-pitfalls)
- [Appendix A: the worked example](#appendix-a-the-worked-example)
- [Appendix B: gate toolkit](#appendix-b-gate-toolkit)
- [Appendix C: Rook documentation references, field names and prior art](#appendix-c-rook-documentation-references-field-names-and-prior-art)
- [Appendix D: evidence status and open items](#appendix-d-evidence-status-and-open-items)

---

## 1. Read this first

### 1.1 Support status

Rook does not support adopting a cephadm cluster. Rook maintainers have said so repeatedly since 2016 (issues 278 and 14971, discussions 12045 and 13792, see Appendix C): they do not run non-Rook clusters, so they cannot test or document a path. We found no public report of anyone completing an in-place cephadm to Rook migration. Rookify (a Sovereign Cloud Stack tool) is the nearest prior art; it is archived, targets ceph-ansible clusters and works differently (it moves daemons while the hosts keep their OS). The `cephadm adopt` direction (legacy to cephadm) is mature and relies on the same on-disk contract this guide relies on, which is a good sign for feasibility but not a support statement.

Treat everything here as "it worked for us, on this hardware, with this lab evidence". You own the risk. The only reason it was acceptable for us is that every step before the final decommission is reversible host by host.

### 1.2 Evidence labels

| Label | Meaning |
|---|---|
| [PROD] | Executed on the production cluster, with a log. |
| [LAB] | Executed in a 4-VM lab (4 cephadm hosts converted to Talos and Rook), not on production. |
| [SRC] | Read in Rook or Ceph source code, not executed. |
| [PLANNED] | Written down, not executed anywhere. |

The headline claim ("zero PG movement") is [LAB] for 4 hosts and [PROD] for 7 hosts: the placement hash was identical at every adoption gate of every window.

### 1.3 Versions

| Component | Version |
|---|---|
| Ceph | 20.2.4 Tentacle in production (the lab ran 20.2.3) |
| Rook | v1.21.0 (chart and image pinned, not bumped during the migration) |
| Talos Linux | v1.14.2 with a small "storage" schematic (CPU microcode and util-linux-tools only) |
| Kubernetes | v1.36.x |
| ceph-csi (kept external) | v3.18.1 |

Version-specific behaviour that matters, so you can check whether it applies to you:

- Ceph 20.2.2 to 20.2.4: on every reconcile Rook runs `ceph orch set backend ""` and disables the `rook` mgr module. Once Rook reconciles, `ceph orch` stops working. Do all `ceph orch` work first. [SRC], confirmed [PROD]. A later Ceph release may drop this rule (unverified).
- Ceph 20.2.4 and later: Rook sets `auth_allowed_ciphers`, `auth_preferred_cipher` and `auth_service_cipher` so that new keys use `aes256k`. These are monmap fields, not config options (`ceph config get mon auth_preferred_cipher` returns ENOENT; use `ceph mon dump` and `ceph mon set`). Existing keys keep their type. Old clients (ceph-fuse 19.2.3) cannot use `aes256k` keys. [PROD]
- Ceph 20.2.3 and later: `ceph-volume lvm activate` deactivates and reactivates the OSD's LVs on every pod start. [SRC]

### 1.4 What "zero data movement" means here

Placement does not change: every PG keeps the same up and acting set, there is no CRUSH change, and no pool starts using a new rule. Recovery of writes that happened while one host was down is normal and not movement; it is what any host reboot causes. The proof is a set of hashes taken before and after each step (section 8 and Appendix B).

### 1.5 Conventions

- `<fsid>`, `<host>`, `<letter>`, `<ceph-ip>` and similar are placeholders.
- Addresses in this repository are documentation ranges: the Ceph network is shown as `192.0.2.0/24` with host `ceph-N` at `192.0.2.2N`, the Kubernetes network as `198.51.100.0/24`. Our real prefixes differ (the Ceph network was a larger prefix than /24, which matters in one routing pitfall).
- Commands that talk to Ceph are shown as plain `ceph ...`. Before Rook existed they ran through `cephadm shell` on a driver host that was not the host being worked on; later through the Rook toolbox (`kubectl -n rook-ceph exec deploy/rook-ceph-tools -- ceph ...`).
- "Window" means the time one host is down for conversion.
- Hold-first: every unexpected result stops the run and goes to a person who decides. Nothing auto-reverts. See [lessons-learned.md](lessons-learned.md).

---

## 2. The idea in one page

A cephadm cluster and a Rook cluster describe the same Ceph cluster with different orchestrators. Ceph itself only cares about a handful of identities. The migration keeps all of them and changes only who starts the processes.

| Invariant | How it is kept | Why it matters |
|---|---|---|
| Same fsid, same `mon.` key, same `client.admin` key | Rook's identity Secret is created by hand from the live values before any CephCluster exists | If Rook cannot read its identity it bootstraps a brand-new cluster. |
| Same mon addresses | Mons are renamed to Rook's letter names under cephadm at the same IPs; Rook's mon ConfigMap is seeded with those letters and IPs | Clients (CSI, hypervisors, SMB, RGW) keep their mon list. |
| Same OSD ids, keys and data | The host is reinstalled with an OS that can see the LVM volumes. Rook's prepare job lists ceph-volume LVM OSDs and keeps every LV whose tag `ceph.cluster_fsid` equals the cluster fsid; it starts the same OSD ids | Nothing is formatted, no OSD is created or destroyed. |
| Same CRUSH host bucket | The Kubernetes node hostname equals the existing CRUSH host bucket name; no topology labels on the node; `osd_crush_update_on_start=false` | A different bucket name would make the OSD re-place itself and cause mass remapping. |
| Same pools, same rules | Rook gets no pool CRs during the migration; any later CR sets values equal to the live ones | Rook must never point a pool at a rule it created. |

Rough shape of the whole project:

| Phase | What happens | Client impact |
|---|---|---|
| 0 | Prove it in a lab, collect baselines, clean up the cluster | none |
| 1 | Rook operator installed. Mons renamed to letters. Rook identity seeded. CephCluster created with no storage nodes. cephadm paused | one mgr module restart |
| 2 | Each host in turn: stop cephadm daemons, reinstall the boot disk, boot it as a Kubernetes node, add one line to the CephCluster, Rook adopts the OSDs and the mon | one host degraded per window |
| 3 | Service daemons move: MDS (handover), RGW (rebuild), SMB (replacement) | MDS failover about 6 to 8 s; S3 offline for an evening window; SMB offline until its replacement |
| 4 | Last host (it carried the awkward services), then remove cephadm | none beyond the above |

Between phases the cluster is healthy and can sit for days. Mixed cephadm and Rook hosts are a normal state.

The key design choice: reinstall each host in place instead of building a second cluster. A second cluster needs a second full capacity set. Rebuilding OSDs under Rook on a cluster with EC k+m equal to the host count leaves no spare failure domain, so each rebuilt host would run one shard short until recovery ends (days on HDDs). Keeping the data on disk and replacing only the boot disk makes each window the same exposure as an ordinary host reboot.

---

## 3. Phase 0: feasibility and lab rehearsal

### 3.1 Is it feasible for your cluster? A checklist

All of these must be true, or be fixed, before Phase 1.

| Check | Why |
|---|---|
| All OSDs are `ceph-volume lvm` OSDs created by cephadm (LV tags `ceph.cluster_fsid`, `ceph.osd_id`, `ceph.osd_fsid`, `ceph.type`, `ceph.db_device` present) | Rook adopts only these. Raw-mode OSDs follow a different path that we did not test. |
| The new OS can activate LVM volume groups at boot | Talos ships lvm2 and activates complete VGs of pre-existing PVs at boot. |
| No size-1 pool, or you accept raising it to size 2 | A size-1 pool blocks Rook's `ok-to-stop` and left the CephCluster Progressing for more than 20 minutes in the lab. Raising a pool's size can hit `mon_max_pg_per_osd` (ERANGE); raise the limit temporarily. |
| You know your failure margin | Our EC 5+2 pool on 7 hosts has `min_size 6`: during a window any second failure makes PGs inactive (I/O blocks, data is safe). We accepted this explicitly. With more hosts than k+m you have slack. |
| `ceph osd ok-to-stop` is true for each host's OSDs | Windows are strictly serial: ok-to-stop is false while another host is down. |
| Each CephFS filesystem keeps a standby that is not on the host being converted | Otherwise converting that host takes CephFS metadata offline. |
| A written inventory of every cephadm service per host | The services (mgr, mds, rgw, ingress, smb, monitoring) do not move by themselves. See [P-14](pitfalls.md#p-14-stale-hand-written-endpoints-and-other-references-after-hosts-convert) and section 6. |
| Every hand-maintained reference to a host's daemons is known | Static Endpoints, haproxy backends, probe targets, DNS records. |
| Backups exist and were verified | `rados export` of the CephFS metadata pools with omap, CephFS journal export, monmap/config/keyring exports (names and caps only in anything you keep online), and an application-level backup (Velero in our case). |
| The orchestrator work is complete before the first Rook reconcile | On Ceph 20.2.2 to 20.2.4 `ceph orch` stops working afterwards. |
| Standing health is understood | List every warning that is expected and why. Anything else is a hold during the migration. |
| Boot media plan per host (spare disk, imaged old disk, or both) | Reusing the old boot disk removes the cheap rollback. We used a new disk per host and kept the old one untouched. |

Also decide before starting:

- Whether the hosts keep their IPs. We kept them (Ceph network on the native VLAN, Kubernetes network as a tagged VLAN on the same switch port). Do not combine a network redesign with the adoption.
- Whether Ceph CSI stays external. We kept the external ceph-csi, set `csi.skipUserCreation: true`, and left the CSI takeover for later.
- Which services Rook will not take over (SMB always; RGW if it runs a realm-less zone).

### 3.2 Why a lab, and what it must contain

We built a lab that copies production quirks, because the first failures are always in the details:

- 4 cephadm VMs (Ceph 20.2.3), EC 2+2 (k+m equals the host count, so one host down sits at `min_size` as in production), 3 Talos control planes and 1 worker, plus a tools VM acting as router and orchestration host.
- Production oddities copied on purpose: a shared DB volume group on an NVMe, hand-named DB LVs, one DB LV spanning two extents, an OSD outside any drive-group spec, SSD-only OSDs, CephFS with an EC default data pool, a second filesystem, a realm-less RGW zone with three placement targets and an ingress VIP, a size-1 pool, `public_network` set.
- Seed data with checksums and canaries: a ceph-fuse writer, a kernel-client reader, an RBD writer and an S3 loop, all logging per second. Measure the noise floor (we saw common-mode stalls of about 4 s under host load with no Ceph event), so stall gates are judged against a baseline.
- A snapshot of every VM at each phase boundary. Gotchas: snapshots do not cover detached disks; keep evidence off the machine you roll back.

Decision gates we used before touching production (G1 to G5): OSD adoption with the same ids and zero remap that survives three reboots and operator restarts; mon quorum never lost and Rook never initiates a mon remove or failover; the first reconcile changes only what we predicted; service failovers inside budget (MDS stall under 60 s); a rehearsed rollback per host. No-go triggers: unexplained remaps, unreliable LVM activation, quorum loss, scrub inconsistencies, Rook behaviour that cannot be disabled.

### 3.3 Lab run history (lesson: the harness can cost more than the migration)

| Run | Outcome | Cause |
|---|---|---|
| 1 to 6 | Aborted or stopped | All tooling defects or false-positive monitor triggers (stale evidence files, mgr letters swapping after a restore, a trigger firing on a planned VM shutdown, a gate that measured the operator's whole reconcile preamble instead of the prepare job, a temp file vanishing during an evidence copy, a gate reading the standby-replay MDS entry instead of the active one). Never a migration fault. |
| 7 | Reverted at the last phase | Enabling Rook's mon health check made Rook treat a `Completed` mon pod next to the live replacement as "two mons on one node" and evict the mon ([P-15](pitfalls.md#p-15-rook-mon-health-check-evicts-a-mon-because-of-a-completed-pod)). A genuine Rook behaviour. |
| 8 | Passed end to end | All four hosts on Talos, all daemons on Rook, CRUSH and placement hashes identical throughout, zero application errors, PG integrity verified. |

Running the final rehearsal phase by phase from a runbook with short, real checks took about 2.5 hours. A strict auto-abort harness took 8.5 hours and died five times on its own plumbing. In production, timing budgets became alerts and pause points, never automatic aborts.

### 3.4 Lab findings worth carrying over

**Rook's default probes kill slow daemons.** On a CPU-starved lab node the default liveness probes killed OSD, mon and mgr pods: 132 degraded, 255 undersized and 72 peering PGs for about 4 minutes. Set explicit loosened liveness and startup probes in the first CephCluster commit (when no Rook daemon exists yet; a later change rolls every daemon). Rook's default startup probe for mon and mgr is 60 s, too short for a freshly adopted mon syncing a 200 MB store.

**ROOK-1.** `kubectl delete node` makes the operator delete that node's crash collector and exporter Deployments. Nothing else happens. Expect it in a per-host rollback.

**ROOK-2 (adopted OSDs restart once).** cephadm leaves an empty `ceph.crush_device_class` LV tag. The OSD Deployments created at adoption therefore have no device class. At the next reconcile Rook adds the class and replaces each affected OSD pod once (Deployment revision 1 to 2). No remap. Reconciles are event driven, so it does not happen by itself; we triggered it deliberately inside each window. In production a few OSDs never bounced: two NVMe OSDs whose class was already set, and two HDD OSDs with a dedicated SSD DB device; their rendered spec already matched. The rule is "at most once per OSD".

**NET-1 (asymmetric routing).** A converted dual-homed node (Ceph network plus Kubernetes network) answered Kubernetes-side clients directly over its Kubernetes leg. The lab's stateful router dropped the half-seen flows and fuse and krbd I/O hung for about 10 minutes until the router was made stateless between the two subnets. Requirements that came out of it: keep storage routing on a stateless L3 device with symmetric routes both ways; keep every Ceph-facing address inside the routed range; never let the storage path fall back to a stateful gateway; and do not copy a fleet-wide static route for (part of) the Ceph range via the switch onto a dual-homed storage node (if it is more specific than the storage node's connected Ceph prefix, it wins by longest prefix and sends Ceph-to-Ceph traffic out the wrong VLAN).

**Guards.** Verified before any pool CR and after every step: operator env `ROOK_DELETE_UNUSED_CRUSH_RULES=false`; `csi.skipUserCreation: true` from the first reconcile; no `security.cephx` block; `requireMsgr2` unset; `ceph balancer off` and `ceph osd pool set noautoscale` (measurement guards); `nodelete` on every pool.

**Device-class gate.** A pool CR without `deviceClass` made Rook create a stray class-less CRUSH rule named after the pool. The pool stayed on its old rule (no movement) but the stray rule is dangerous. With `deviceClass` and `failureDomain: host` set equal to the pool's current rule, 8 CR commits plus an operator restart produced zero new rules, zero `crush_rule` changes and zero PG movement. Gate name POOLRULE: the `crush_rule` of every pool is unchanged after every step.

**"Stop managing" escape hatch.** Deleting a CephFilesystem CR runs `ceph fs fail`. The only way to make Rook let go of a CR without deleting the resource is: scale the operator to 0, strip the finalizer, delete with `--cascade=orphan`. With the operator running, stripping the finalizer triggers a reconcile that races to re-add it, and if Rook wins, the delete path runs. Rehearsed only on a throwaway filesystem and pool. The preferred undo for any CR is to revert the Git commit and re-apply (rehearsed on a copy of the real filesystem: idempotent, about 6 minutes).

### 3.5 What you must not skip: baselines and the hash method

Before any change, with the balancer off and `noautoscale` set (otherwise upmap and `pg_num` changes look like Rook damage):

```bash
ceph balancer off
ceph osd pool set noautoscale
# re-baseline after both: they change the osdmap flags string and one config row
```

Then capture a baseline (we called it B0) containing at least: health codes, quorum and monmap (epoch, names, addresses), PG states, `ceph versions`, OSD flags and counts and the blocklist count, pool details, CRUSH dump, rule dump, `osd tree`, the placement hash, auth entities as `sha256(key):sha256(caps)` (never the keys), the `mon.` key hash, the config dump (hash secret-looking values), mgr and orch state, filesystem state. Appendix B shows how each hash is computed, and [examples/scripts/zero-movement-gate.py](../examples/scripts/zero-movement-gate.py) is a working gate. Take a second baseline 15 minutes later and compare it with the first, to prove the gate itself is stable. Every later step is diffed against the previous baseline, with a short list of differences that step is expected to cause.

### 3.6 Clean-ups before Phase 1

Cheap, independent of Rook, and worth doing first: remove stale daemon entries and failed units, fix hosts with missing or mismatched `cephadm` or ceph packages, delete empty unused pools (under a short, pool-scoped lifting of `mon_allow_pool_delete`), delete orphan RBD images, rotate leaked service keys, make sure no pool has size 1.

---

## 4. Phase 1: Rook alongside cephadm, nothing moves

Scope: install the operator, rename the mons to Rook's letters at the same IPs, seed Rook's identity, create a CephCluster with no storage nodes, pause cephadm. After this phase every Ceph daemon is still a cephadm systemd container, all OSDs are untouched, mon IPs are unchanged, and cephadm no longer orchestrates. [PROD], executed over two days (about 8 hours wall clock because of holds; planned 2.5 to 3.5 hours).

### 4.1 Step 1: the operator, with CSI off, and no CephCluster

Deliver it through your GitOps tool if you have one; the pattern that mattered is the "never prune" settings. Examples: [examples/flux/](../examples/flux/).

- Namespace `rook-ceph` with Pod Security `enforce/audit/warn: privileged` (mon, OSD and prepare pods use hostPath and privileged mode). Keep the namespace and the HelmRelease from being pruned (in Flux, the annotation `kustomize.toolkit.fluxcd.io/prune: disabled`), because deleting the namespace would delete every Rook CR plus the identity Secret.
- Helm chart and image pinned to the same version (`image.tag: v1.21.0`). Do not bump Rook during the migration; your lab evidence is for one version only.
- Helm values that matter:

```yaml
image: {tag: v1.21.0}
crds: {enabled: true}                 # main CRDs carry helm.sh/resource-policy: keep
logLevel: INFO
resources: {requests: {cpu: "200m", memory: "256Mi"}, limits: {memory: "1Gi"}}   # sized for ~44 OSDs and 7 mons
deleteUnusedCrushRules: false         # becomes ROOK_DELETE_UNUSED_CRUSH_RULES; the default (true) lets the operator delete rules it thinks unused
enableDiscoveryDaemon: false
csi:
  installCsiOperator: false           # external ceph-csi stays
  createCsiOperatorResources: false   # not in values.yaml, but the chart template reads it
```

- The six `csi.ceph.io` CRDs (cephconnection, clientprofile, clientprofilemapping, clientprofilereplication, driver, operatorconfig) are required even with CSI off. With `installCsiOperator: false` the chart omits them and the v1.21.0 operator crash-loops waiting for the ClientProfile cache. Apply them from a separate Kustomization using server-side apply (about 1.1 MB, too big for client-side apply annotations).
- Flux Kustomizations for Rook objects use `prune: false`, `deletionPolicy: Orphan` and `force: false` from the start. Reason: a common habit for archiving an app is to drop its entry from the parent list, and a parent with `prune: true` would cascade-delete the child's objects, including filesystem CRs. `force: false` prevents delete-and-recreate on an immutable-field conflict (recreating a CephFilesystem runs `fs fail`). Prove the brake with a sentinel ConfigMap, never with a real CR.
- Pre-commit checks: `kubectl kustomize` builds; `kubectl apply --dry-run=server --validate=strict`; `helm template` compared with the cluster's existing RBAC names, CRDs and ServiceAccounts (we found no collisions with an external ceph-csi, which uses different names).

Gate after 15 minutes of operator uptime: operator 1/1 with 0 restarts, no `CephCluster` exists, zero error lines in the log, the Ceph gate is identical to the baseline. And one test that is easy to forget: **reachability of every mon on 3300 and 6789 from the pod network.** An existing external ceph-csi usually runs on the host network, so its working proves nothing about the pod network. We ran one short-lived busybox pod per node type doing `nc -z` to all mons on both ports and read the v1 banner.

Rollback: revert the commit, delete the HelmRelease, the Rook CRDs and the namespace by hand. No Ceph impact.

### 4.2 Step 2: freeze cephadm scheduling

Stop cephadm from re-creating a mon you remove, and from touching anything else:

```bash
ceph orch ls --format json > managed-services.json          # keep the list
ceph orch ls --export > orch-ls-export.SECRET.yaml            # may contain service secrets; mode 600, stays on the host
ceph orch set-unmanaged node-exporter                         # canary: wait 2 min, run the full gate, expect zero daemon restarts
for s in <every service except mon>; do ceph orch set-unmanaged "$s"; done
ceph orch set-unmanaged mon                                   # LAST
ceph orch ls | awk '{print $1,$NF}'                           # expect every service <unmanaged>
```

`set-unmanaged` only flips `spec.unmanaged` in the mgr store and does not restart, reconfigure or redeploy any daemon of any service type (cephadm 20.2.4 source). Never run `ceph orch rm`, or `ceph orch apply` with a changed spec, in this phase: orphan removal triggers when a spec is missing, not when it is unmanaged. [PROD]: 25 services set unmanaged, zero daemon restarts.

### 4.3 Step 3: rename the mons to Rook letters, at the same IPs

**Why.** Rook names mons `a`, `b`, ... (`IndexToName`, letters only) and tracks them in a ConfigMap with `maxMonId`. cephadm names them `mon.<hostname>`. Rook can load other names but fails when committing `maxMonId` (`invalid mon name`), and a mon added later takes a letter derived from `maxMonId`. Renaming under cephadm first keeps the IPs, so every client's mon list stays valid. `ceph orch apply mon --placement=<host>=<name>` does NOT rename in place (a dry run showed adds before removes); do not use it.

**Mapping.** Any mapping works as long as the seed (4.4) matches the live monmap. We used `a` = ceph-1 through `g` = ceph-7. Order: healthy boot media first, hosts with failing boot SSDs last, the current mon leader last (a leader rename causes one re-election). Strictly one mon at a time with the full gate in between. With 7 mons each rename leaves 6 of 7 in quorum, a margin of two.

**Raise `paxos_max_join_drift` before the first rename** (see "Stall 1" below). It cost us the first 15-minute stall.

**One rename iteration** (run from a host other than the one being renamed, because `cephadm shell` infers its config from the local mon directory, which the rename moves):

```bash
ceph orch daemon rm mon.<host> --force            # wait for quorum 6/6 and the old name to vanish (4-11 s)
ceph orch daemon add mon <host>:<ip>=<letter>     # e.g. ceph-6:192.0.2.26=f ; wait for quorum 7/7
ceph mon dump | grep -E 'epoch|mon\.'             # expect [v2:<ip>:3300/0,v1:<ip>:6789/0]
```

Facts about these commands (cephadm 20.2.4 source and lab):

- `rm` checks that the remaining quorum is still a majority, runs `ceph mon rm`, then `cephadm rm-daemon --force`. The old mon store is moved, not deleted, to `/var/lib/ceph/<fsid>/removed/mon.<host>_<timestamp>` (about 200 MB). It cannot be restored into the cluster because the monmap moved on.
- `add` uses the existing shared `mon.` key (no new key), writes `public addr = <ip>` into the new mon's config, deploys, and the mon joins after a full store sync. **The monmap epoch advances by 2 per rename.**
- The original cephadm mons carry `public network = <list>`; a renamed mon carries `public addr = <ip>`. This pins it to the host IP and binds both v2 :3300 and v1 :6789 (important if a host also carries a secondary VIP in the same subnet).
- `ceph orch daemon rm -h` is swallowed by the generic `ceph -h`. Call the command with no arguments to see its usage.

Per-rename gate: quorum 7/7; mon names and addresses exactly as expected, two addresses each; epoch exactly +2; all PGs active+clean; placement hash, CRUSH, rules, pools, OSD flags and state, auth hashes and the `mon.` key hash identical to the baseline; no other daemon restarted on any host; ceph-csi pod restart counts unchanged; blocklist count unchanged. Budget (a tiny lab store took 24 to 34 s; our production stores were 200 MB): 6/6 within 60 s, 7/7 within 120 s, hard ceiling 15 minutes then HOLD.

#### What actually happened in production [PROD]

Three of the seven renames needed intervention. The renames that worked took 7 to 61 seconds. The cause of the stalls is only partly understood, so this section says what we saw and what fixed it.

**Stall 1: the new mon never joined (paxos join drift).** The first renamed mon sat in `probing` with rank -1, store synced, listening on 3300 and 6789, connected to every peer, monmap unchanged for 15 minutes. Nothing appears in `ceph -s` or at default log levels. With `debug_mon 10/10` on the new mon for 25 s, every probe cycle (about 2 s) logged a line of the form:

```
peer paxos last version <N+11> vs my version <N> (too far ahead)
```

and restarted the sync. Root cause: the new mon syncs the 200 MB store in about 2 s, but a busy cluster commits more than the allowed number of paxos versions (default 10) in that time, so the mon never reaches "ready to join" and never sends `mon_join`. The lab (idle cluster, tiny store) never showed it. Fix, temporary: `ceph config set mon paxos_max_join_drift 100`. The option name is `paxos_max_join_drift`; `mon_paxos_max_join_drift` does not exist (ENOENT). Our helper wrapper discarded stderr, so the first, misspelled `config set` failed silently and cost 5 minutes: always read the value back with `ceph config get mon paxos_max_join_drift`. Remove it after the last rename (`ceph config rm mon paxos_max_join_drift`). While it is set, your gate needs an allowance for the config row.

**Stall 2: join requests ignored while one particular mon was leader.** After the drift fix the mon logged `ready to join ... trying to join` every 2 s and sent `mon_join` to the leader, but the leader never logged receiving it. The second rename stalled the same way for 15 minutes. Fix that worked immediately: move leadership off that mon.

```bash
ceph mon set election_strategy disallow
ceph mon add disallowed_leader <leader-name>      # the leader moved and the new mon joined at once
# after the last rename:
ceph mon set election_strategy classic            # verify election_strategy 1 and an empty disallowed_leaders list
```

The disallowed mon stays a normal voting member and can still be removed by `orch daemon rm` (that also clears it from the list). The hypothesis "that mon as leader was the problem" is not proven: a later rename stalled under a different leader. Treat this as an observed workaround.

**Stall 3: a stuck join under a different leader; force-add.** The fifth rename (slowest host, `cephadm deploy` alone took 33 s) did not join in 15 minutes under a different leader. Fix: add the mon to the monmap yourself.

```bash
ceph mon add <letter> <ip>        # bare IP, no port, no brackets: registers both v2:3300 and v1:6789
```

The bracketed address-vector forms (`[v2:ip:3300,v1:ip:6789]`, and the `/0` nonce form) crash the 20.2.4 Python CLI with an IndexError in `ceph_argparse` (it parses the bracket form as an IPv6 literal); nothing reaches the cluster. Use the bare IP after about 3 minutes of "not in quorum". Side effect: a stale `CEPHADM_STRAY_DAEMON` health warning ("stray daemon mon.<old-name> on host X") that persists until cephadm is disabled. Allowlist it, including in any helper script's own precheck defaults.

**Epoch accounting.** The monmap epoch went 21 to 38: 14 for seven renames, 2 for the strategy changes (`disallow` and `disallowed_leader`), 1 for returning to `classic`. Rook's first reconcile later added 1 (cipher change), giving 39, and it then stayed at 39 through every host window.

**Other things that happened in that session.**

- The leader's host hard-reset mid-run (the BMC event log showed a host power-control event in the same second as a routine IPMI-over-LAN call from a fan-controller pod). Cause unconfirmed. 5 OSDs and a mon were down for about 3 minutes, 806 PGs went active+undersized+degraded, none inactive, client I/O continued, recovery of the outage writes finished in minutes. Lesson: a mon rename session is a bad time for unrelated host instability; have an explicit hold rule on any unexpected host reset; do not run verbose debug on the leader.
- cephadm's memory autotune re-measured the reset host and changed the config row `osd/host:<h> osd_memory_target`, making a strict config-diff gate fail although nothing relevant had changed. Allow that row by regex.
- A helper that passed an empty argument made `argparse` exit 2; a shell syntax error in an edited helper aborted one iteration before anything ran. Test helpers before the session.

Rename rollback: another fresh-store rename (`orch daemon rm mon.<letter> --force`, then `orch daemon add mon <host>:<ip>=<host>`), another +2 epoch. In practice stop and stay: letter-named mons are valid under cephadm.

### 4.4 Step 4: seed Rook's identity (by hand, never through Git)

Create these before the CephCluster exists. If Rook cannot read them it bootstraps a new cluster with a new fsid (precedent: rook#4274 in 2019). Templates: [examples/seed/](../examples/seed/).

**Secret `rook-ceph-mon`** (namespace `rook-ceph`, type `kubernetes.io/rook`) with four keys:

| Key | Content | Source |
|---|---|---|
| `fsid` | cluster fsid, no trailing newline | `ceph fsid` |
| `mon-secret` | the `mon.` cephx key | `ceph auth get-key mon.` |
| `ceph-username` | literal `client.admin` | constant |
| `ceph-secret` | the `client.admin` key | `ceph auth get-key client.admin` |

Note on sources: a summary of Rook's disaster-recovery page implies `mon-secret` is the admin key; the code (`cluster_info.go`) reads it as the `mon.` key, and the `mon.` key is retrievable on cephadm clusters. We used the code reading and verified the `mon.` key hash against the baseline.

Never put key values on a command line, in shell history or in a file you commit. Pattern we used: `umask 077`; fetch each value over SSH into a temp file with `tr -d '\r\n'`; verify by length and hash only; `kubectl create secret generic rook-ceph-mon --type=kubernetes.io/rook --from-file=fsid=... --from-file=mon-secret=... --from-file=ceph-username=... --from-file=ceph-secret=...`; check lengths with a go-template over `.data`; blank and delete the temp files.

**ConfigMap `rook-ceph-mon-endpoints`:**

```yaml
data:
  data: "a=192.0.2.21:6789,b=192.0.2.22:6789,c=192.0.2.23:6789,d=192.0.2.24:6789,e=192.0.2.25:6789,f=192.0.2.26:6789,g=192.0.2.27:6789"
  mapping: '{"node":{"a":{"Name":"ceph-1","Hostname":"ceph-1","Address":"192.0.2.21"}, ... "g":{"Name":"ceph-7","Hostname":"ceph-7","Address":"192.0.2.27"}}}'
  maxMonId: "6"        # 0-based index of the highest letter: a=0 ... g=6; Rook never reuses an id
```

- Port 6789 in `data` makes Rook advertise both v2 :3300 and v1 :6789. Port 3300 would mean v2 only. Rook never runs `ceph mon enable-msgr2`.
- `mapping[].Hostname` becomes a `kubernetes.io/hostname=<Hostname>` nodeSelector on the mon Deployment. Since no Kubernetes node has that name yet, the seven mon Deployments exist with Pending pods. That is the trick of this phase: Rook knows the mons, cannot start them, and does not need to.
- A mon present in `data` but missing from `mapping` becomes a pod-network mon with a ClusterIP advertising the old IP. Wrong. Keep both in sync.
- Gate before applying: compare the live `ceph mon dump -f json` (name=ip:6789, sorted) with the `data` string, count seven `"Hostname":` entries, check `maxMonId`.
- Optional Secret `rook-ceph-dashboard-password` (key `password`). Without it Rook generates a random password and resets the dashboard `admin` user to it on each reconcile; other dashboard users are untouched.

Then a **connectivity gate from the pod network**: a one-shot pod using the exact running Ceph image that mounts the endpoints ConfigMap, builds a temporary `ceph.conf` and keyring from the Secret, and runs `ceph -s` and `ceph versions`. Expected output equals the cluster's own `ceph -s`. This pre-pulls the roughly 1.4 GB image (the detect-version Job pulls it on first reconcile anyway) and is the first large-frame (MTU 9000) test across the pod network.

### 4.5 Step 5: the CephCluster with no storage nodes

Deliver it as one commit. Annotate the object with the never-prune annotation; use `wait: false` on its Kustomization (Rook owns the status and shows Progressing for minutes); never delete this CR (with `cleanupPolicy` empty the data survives, but every Rook daemon is torn down). All later changes are Git commits: a `kubectl patch` of a GitOps-owned CR is reverted at the next reconcile and rolls Rook again. Full example with comments: [examples/cephcluster/cephcluster.yml](../examples/cephcluster/cephcluster.yml).

```yaml
apiVersion: ceph.rook.io/v1
kind: CephCluster
metadata:
  name: rook-ceph
  namespace: rook-ceph
  annotations: {kustomize.toolkit.fluxcd.io/prune: disabled}
spec:
  cephVersion:
    image: quay.io/ceph/ceph@<digest of the running image>   # digest, not tag; must equal what cephadm runs
    allowUnsupported: false
  dataDirHostPath: /var/lib/rook          # immutable
  skipUpgradeChecks: false
  continueUpgradeAfterChecksEvenIfNotHealthy: false
  network:
    provider: host
    addressRanges:
      public: ["<existing mon public_network>"]   # no cluster range if you have no cluster_network; do not set requireMsgr2 or encryption
  mon: {count: 7, allowMultiplePerNode: false}
  mgr:
    count: 2
    allowMultiplePerNode: false
    modules: [{name: iostat, enabled: true}, {name: snap_schedule, enabled: true}]   # never list dashboard or prometheus
  dashboard:
    enabled: true
    ssl: true
    port: 8443
    prometheusEndpoint: "<current PROMETHEUS_API_HOST value>"
    prometheusEndpointSSLVerify: true
  crashCollector: {disable: false}
  healthCheck:
    daemonHealth: {mon: {disabled: true}}
    livenessProbe:      # mon, mgr, osd each
      mon: {probe: {initialDelaySeconds: 10, timeoutSeconds: 30, periodSeconds: 30, failureThreshold: 10}}
      # mgr and osd: same values
    startupProbe:
      mon: {probe: {initialDelaySeconds: 10, timeoutSeconds: 30, periodSeconds: 10, failureThreshold: 60}}   # 10 min
      mgr: {probe: {initialDelaySeconds: 10, timeoutSeconds: 30, periodSeconds: 10, failureThreshold: 30}}   # 5 min
      # osd: Rook default (720 = 2 h)
  cephConfig:
    global: {osd_crush_update_on_start: "false"}
    mon: {mon_allow_pool_delete: "false"}
  csi: {skipUserCreation: true}
  placement:
    mon: {tolerations: [{key: ceph-storage-node, operator: Exists}]}
    mgr:        {nodeAffinity: <required ceph-storage-node In ["true"]>, tolerations: [{key: ceph-storage-node, operator: Exists}]}
    osd:        {nodeAffinity: <same>, tolerations: <same>}
    prepareosd: {nodeAffinity: <same>, tolerations: <same>}
    cleanup:    {nodeAffinity: <same>, tolerations: <same>}
  storage:
    useAllNodes: false
    useAllDevices: false
    allowDeviceClassUpdate: false
    allowOsdCrushWeightUpdate: false
    nodes: []
  removeOSDsIfOutAndSafeToRemove: false
  cleanupPolicy: {confirmation: ""}      # never "yes-really-destroy-data"
  disruptionManagement: {managePodBudgets: false}
  resources:
    mon: {requests: {cpu: "500m", memory: "1Gi"}, limits: {memory: "2Gi"}}
    mgr: {requests: {cpu: "500m", memory: "1Gi"}, limits: {memory: "2Gi"}}
    # OSD resources are set per node later; do NOT set osd-hdd/osd-ssd/osd-nvme (they override node-level resources)
```

Why each field is the way it is:

| Field | Reason |
|---|---|
| `cephVersion.image` equals the running image digest | A different build, even at the same version, is classified as an upgrade: health checks apply, `isUpgrade` makes mon start failures abort, and MDS upgrade logic can run. Verify that `ceph versions` shows one string per daemon type and that `ceph --version` in the CR image prints the identical commit. |
| `storage.nodes: []`, `useAllNodes/useAllDevices: false` | No OSD adoption in this phase. |
| `mon.count` equals the number of seeded mons; `allowMultiplePerNode: false` | `preClusterStartValidation` lists all Node objects (no filter) and fails only when there are fewer nodes than the mon count. 7 mons with 7 Nodes passes with zero margin. **Do not delete a Node object during the phase.** Do not use `allowMultiplePerNode: true` (a failover could double mons on one node). |
| `healthCheck.daemonHealth.mon.disabled: true` | The mon pods are Pending by design. An enabled health check would try to fail them over to ordinary Kubernetes nodes, immediately if the mapped hostname has no Node. Re-enable only when all mons are Rook pods (section 7). |
| `mgr.count: 2` | Rook runs one mgr per node, so a second mgr stays Pending until two storage nodes exist. Never list `dashboard` or `prometheus` in `modules`. |
| `dashboard.enabled: true` plus `prometheusEndpoint` | `enabled: false` makes Rook run `mgr module disable dashboard`. Rook writes `PROMETHEUS_API_HOST` unconditionally from the CR field (unset blanks it) and `PROMETHEUS_API_SSL_VERIFY` from the bool; with both set to current values no write is issued. Rook also replaces the self-signed TLS certificate and resets `admin`'s password from the Secret. `ALERTMANAGER_API_HOST` and `GRAFANA_API_URL` are untouched unless you set them through `cephConfig.mgr`. |
| Loosened probes in the first commit | See 3.4. |
| `cephConfig.global.osd_crush_update_on_start: "false"` | Makes `--crush-location` inert so a hostname mismatch cannot move OSDs. Protects OSDs whose CRUSH class was set explicitly (our NVMe OSDs report `default_device_class ssd` but sit in class `nvme`). Rook never unsets a key it set, so removing it later needs the CR edit and `ceph config rm global osd_crush_update_on_start`. |
| `cephConfig.mon.mon_allow_pool_delete: "false"` | Rook rewrites the global `mon_allow_pool_delete=true` and `mon_allow_pool_size_one=true` on every reconcile; the more specific `mon` section wins. Add `nodelete` on every pool as a second brake. |
| `csi.skipUserCreation: true` from the first reconcile | Otherwise Rook rewrites caps on pre-existing CSI users. Not on the public CephCluster page; from source (`types.go`) and lab. |
| No `placement.all` | The `rook-ceph-detect-version` Job takes the mon placement merged with `all`. A required storage-node affinity there leaves the Job Pending (no storage node exists yet) and stalls the reconcile. Put affinity and tolerations on `mgr`, `osd`, `prepareosd` and `cleanup` only. |
| Mon pin not set yet | The nodeAffinity pin for mons comes in a later commit, after the first storage node is Ready (section 5). |
| `network.provider: host` and `public` range | Daemons bind the host's Ceph-network address like cephadm did; mon IPs come from the seed. See the `public_network` side effect in 4.6. |
| No `requireMsgr2` | Old kernel clients exist. |
| `removeOSDsIfOutAndSafeToRemove: false`, `cleanupPolicy.confirmation: ""`, `managePodBudgets: false` | Rook must not remove or wipe anything. Rook PDBs would also hold node drains. |

Before pushing, run `kubectl apply --dry-run=server --validate=strict`. Unknown CRD fields are otherwise silently pruned.

### 4.6 What the first reconcile does to Ceph

Observed in production after pushing the CR (Ready 8 minutes after the push, inside a 12-minute budget):

| Area | Change |
|---|---|
| Rook objects | 7 mon Deployments, 2 mgr Deployments (9 Pending pods), a `rook-ceph-mgr-pdb` PodDisruptionBudget with zero allowed disruptions even with `managePodBudgets: false` (it blocks `kubectl drain` of a node running a Rook mgr). No OSD, no prepare job. |
| auth | Deleted `client.bootstrap-osd` (the only bootstrap key that existed; cephadm needs it only to create OSDs). Added `mgr.a`, `mgr.b`, `client.crash`, `client.ceph-exporter`, `client.rbd-mirror-peer`. No other entity's key or caps changed. |
| config | Added `global/log_to_file=false`, `global/mon_cluster_log_file=""`, `global/osd_crush_update_on_start=false`, `global/public_network=<range>`, `mgr/dashboard/server_port`. Removed `mgr/orchestrator/orchestrator`. |
| monmap | `auth_preferred_cipher` aes to aes256k, epoch +1. Existing keys are untouched. |
| orchestrator | `ceph orch` answers "No orchestrator configured". |
| mgr | The active cephadm mgr module respawns once (about 3 s; the container does not restart). |
| osd blocklist | 14 entries for the old mgr's client instances, 24 h expiry. Your gate needs an allowance, and later gates need it again or a re-baseline. |
| Everything else | Placement hash, CRUSH, rules, pools, `nodelete`, OSD state, mon names, `mon.` key hash, cephadm daemon start times except the mgr: identical. |

Timeline: the reconcile blocks about 5 minutes at "Configuring Ceph Mgr(s)" ("gave up waiting for deployment"; Pending mgr pods are waited on, Pending mons are not), logs one expected error, retries about 30 s later and ends "Cluster created successfully". Tell everyone the 5-minute stall is expected.

**`public_network` side effect.** `network.addressRanges.public` makes Rook write `global/public_network`. On a host that carries a secondary VIP inside that range (ours: a keepalived VIP for the RGW ingress) a cephadm daemon restarting could bind the VIP. We pinned host-scoped addresses: `ceph config set osd/host:<h> public_addr <host-ip>` (and `mds/host:`, `mgr/host:`). `public_addr` is read at startup, so nothing restarted. Remove the pins when that host converts or the VIP moves (we forgot this once and removed them at decommission).

**Pause cephadm.**

```bash
ceph orch pause                                   # raises CEPHADM_PAUSED, a standing HEALTH_WARN: allowlist it
ceph config set mgr mgr/smb/update_orchestration false   # only if you use the smb module; read once at module load
```

`pause` only sets a flag; host refresh and the stray-daemon check still run, which is why `CEPHADM_STRAY_*` and `CEPHADM_HOST_CHECK_FAILED` keep appearing and grow with each conversion. Standing rules from here to the end: no `ceph orch` commands, no `ceph smb` or `ceph nfs` changes, no cephadm upgrade, no new service, no host add or remove, no OSD add or replace via cephadm. The existing daemons are plain systemd units: restart with `systemctl restart ceph-<fsid>@<type>.<id>.service`, read logs with `journalctl -u ...` or `cephadm logs --fsid <fsid> --name <daemon>`, stop a host's whole set with `systemctl stop ceph-<fsid>.target`. A disk replacement during the migration needs a hand-run `ceph-volume` with `client.bootstrap-osd` recreated and the operator scaled to 0 (Rook deletes that key every reconcile).

Never run `ceph orch host rm --offline --force` anywhere in this project: it runs `osd purge-actual` on every OSD of the host (ceph 20.2.3 `module.py`).

### 4.7 Phase 1 gate, soak and rollback

Gate after the push: CephCluster Ready; 9 Pending Rook pods; operator 0 restarts and quiet; no `mon remove`, `failover` or `MMonJoin` in the operator log; `rook-ceph-mon-endpoints` unchanged; every client key hash identical; dashboard answers on the active mgr; `systemctl --failed` clean on all hosts; metrics scrape back within 10 minutes; the placement hash identical. Hold triggers: any mon change, any OSD down, any auth change outside the table above, a Rook pod unexpectedly Running (a mon pod scheduled on a Kubernetes node means the hostname nodeSelector failed), a second restart of the active mgr, CephCluster Progressing for more than 25 minutes. Soak: we gated every few hours and every later window re-checked the same state.

Rollback after the push (45 to 60 minutes, zero data movement; never run end to end, so [PLANNED]): operator to 0; revert the commit; strip finalizers and delete the CephCluster, the mon and mgr Deployments, the PDB and the `ceph.rook.io/disaster-protection` finalizers on the seed Secret and ConfigMap; `ceph orch set backend cephadm`; recreate the bootstrap key with `ceph auth get-or-create client.bootstrap-osd mon 'allow profile bootstrap-osd'` (a new key); `ceph config rm` the keys Rook added; `ceph mon set auth_preferred_cipher <old>`; `ceph orch resume`. Cannot be undone, all harmless: the epoch bump, the `removed/` store copies, config history, Rook's `config-key rook/*` entries, the regenerated dashboard certificate.

---

## 5. Phase 2: per-host conversion

One host at a time. [PROD]: seven hosts over four days. Each host: stop the cephadm daemons, replace the boot disk and install Talos, join the Kubernetes cluster, add one line to the CephCluster, and Rook starts the same OSDs and the same mon on it. The full procedure, gates, rollbacks and timings are in **[host-conversion.md](host-conversion.md)**. The short version:

| Step | What |
|---|---|
| P0 | Baselines and preflight gates (`ok-to-stop`, standby MDS elsewhere, LV tags, hash baseline) |
| P1 | Open the window: quiesce heavy writers ([flux-freeze.md](flux-freeze.md)), silences, `ceph osd set-group noout <host>` |
| P2 | Switch port trunk (Ceph VLAN native, Kubernetes VLAN tagged) and boot media staged on the BMC |
| P3 | Stop the cephadm target on the host (T0), window gate within 60 s |
| P4 | Shutdown, swap the boot disk only, hardware boot recipe ([dell-13g-talos-boot.md](dell-13g-talos-boot.md)) |
| P5 | Maintenance-mode gate, then install Talos onto the new disk selected by WWN |
| P6 | Cold-boot proof, network, jumbo-frame and ARP gate from an ordinary worker |
| P7 | Add-node commit; Rook adopts the OSDs and the mon; zero-movement gate |
| P8 | Planned ROOK-2 bounce, unset `noout`, remove the cephadm memory mask, soak |

After the first storage node is Ready, add the mon placement pin (`placement.mon.nodeAffinity` requiring `ceph-storage-node In ["true"]`) as its own commit, outside any window. It rolls the running Rook mons one at a time. Without it a Rook mon could be failed over onto an ordinary Kubernetes node (lab run 7). [PROD]

---

## 6. Phase 3: service daemons

Rook takes over OSDs and mons in phase 2. The other cephadm services (mgr, MDS, RGW, ingress, SMB, monitoring) do not move by themselves; each needs its own handover.

| Item | State |
|---|---|
| Rook mgr a and b | [PROD] standbys from phase 1; a Rook mgr became active during the conversions (6.1) |
| MDS (both filesystems) | [PROD] [mds-adoption.md](mds-adoption.md) |
| RGW | [PROD] rebuilt as a new store: [rgw-rebuild.md](rgw-rebuild.md) |
| SMB | [PROD] replaced by a sambacc pod: [smb-replacement.md](smb-replacement.md) |
| Pools and filesystems as full CRs (beyond MDS-only) | [LAB] for 19 lab pools and both filesystems; [PLANNED] for production (6.2) |
| CSI takeover | [LAB]; production keeps the external ceph-csi (6.3) |
| Monitoring stack | cephadm's Prometheus/Grafana/Alertmanager retired with their host; the dashboard was pointed at an external Prometheus first (6.3) |

### 6.1 The mgr

**Designed sequence** ([LAB] run 8, [SRC]): with at least two Rook storage nodes, set `mgr/smb/update_orchestration false` if you use the smb module, **disable the cephadm module before stopping any cephadm mgr** (`ceph mgr module disable cephadm`; otherwise the cephadm serve loop runs inside the Rook mgr pod), stop the cephadm standbys, `ceph mgr fail` the active so a Rook mgr takes over, then stop the old active.

**What happened in production** [PROD]: we did not run that sequence. During phase 2 the active mgr moved between cephadm mgrs each time its host was stopped, and later a Rook mgr became active while a cephadm standby was still around (that standby then crashed on a port clash, [P-20](pitfalls.md#p-20-mgr-dashboard-port-clashes)). The cephadm module was still enabled at that point, but paused and with the orchestrator backend already cleared by Rook. The visible effect was that `CEPHADM_HOST_CHECK_FAILED` listed every converted host ("connect call failed ... port 22", there is no SSH on Talos); nothing was changed on any host. The module was disabled at decommission (section 7) without a mgr failover. We would still follow the designed sequence next time; we got away with the paused module, we did not prove it safe.

Expect after any mgr change: the dashboard and the prometheus module move with the active mgr (the prometheus module is served only by the active mgr, so a static scrape target silently goes flat; scrape every mgr or go through a Service that follows the active one); a freshly active mgr has no PG stats for about 5 s (`pg ls`, `pg dump_stuck`, `crash ls` can fail or return empty; gates must tolerate it).

What Rook does to the mgr on every reconcile: clears the orchestrator backend (on 20.2.2 to 20.2.4); creates the key with `get-or-create-key mgr.<id>`; resets the dashboard `admin` password from the Secret; enables the prometheus module unless metrics are disabled; creates the zero-disruption PDB. Mgr `fs volume` and `subvolumegroup` commands can hang after any filesystem-offline event; keep `ceph mgr fail` ready (sometimes needed twice) and use command timeouts.

### 6.2 Pools and filesystems as CRs (beyond MDS-only) [LAB], [PLANNED] for production

This is optional: Rook runs happily with unmanaged pools. We plan it for the end state, one CR per commit, and rehearsed it in the lab on all 19 pools and both filesystems with 833 PGs clean after every step. The adoption contract:

1. **One CR file per Git commit.** Force a reconcile, run the whole gate set, hold (never auto-revert) on any red. Preferred undo: revert the commit and re-apply. Never `osd pool set` by hand on an adopted pool (Rook re-applies the CR; scale the operator to 0 for manual maintenance).
2. **POOLRULE gate** after every CR: every baseline pool keeps its `crush_rule`; `osd crush rule ls` may grow only by class-correct, pool-named, unreferenced rules. Never switch a pool onto a Rook-created rule. With `enableCrushUpdates` unset, Rook never re-points an existing pool.
3. **Every pool CR sets `deviceClass` and `failureDomain: host` equal to the pool's current rule.** In the lab this form produced no new rules. Rehearse with a rule class you actually have.
4. **No `pg_num`, autoscale, quotas, `compressionMode`, `enableCrushUpdates`, `replicasPerFailureDomain`, `mirroring` or `targetSizeRatio`** in any CR unless you chose it. Omitted quotas are kept; omitted `parameters` leave autoscale and `pg_num` alone. Do not set `compressionMode` on a pool with live compression, not even `none`.
5. **Size assert before the commit:** every `size` and EC `k,m` in the CR equals the live pool, because a different value makes Rook run `osd pool set <pool> size N` (re-replication). Extra brake: `nosizechange` on the pools with CRs.
6. **`nodelete` on every pool** and `mon_allow_pool_delete=false` on the mons before the first CR (Rook sets the global flag to true).
7. **Never delete a CR.** Escape hatch: operator to 0, strip the finalizer, `--cascade=orphan` (rehearsed only on throwaways).
8. All sizing in the manifest; patches are reverted.

Shapes:

```yaml
# CephBlockPool: adopt an existing RBD pool
spec:
  failureDomain: host
  deviceClass: ssd                      # nvme for NVMe pools
  replicated: {size: 3, requireSafeReplicaSize: true}
  enableRBDStats: true                  # only if the pool is in mgr/prometheus/rbd_stats_pools; dropping the field later REMOVES the key
```

- Built-in pools use `metadata.name: builtin-mgr` plus `spec.name: .mgr` (a Kubernetes object name cannot start with a dot). Only `.rgw.root`, `.nfs`, `.mgr`, `.nvmeof` are allowed for `spec.name`. `.smb` cannot be adopted on v1.21.0 (not in the enum).
- A CephFilesystem in the fully adopted form adds `preservePoolNames: true` (mandatory when pool names differ from `<fs>-metadata` / `<fs>-<name>`; otherwise Rook computes names and would create a missing pool), `preserveFilesystemOnDelete: true`, `preservePoolsOnDelete: true`, a `metadataPool` with name, failure domain, device class and replication, and `dataPools` with the live values. Set `preserveFilesystemOnDelete` explicitly: `preservePoolsOnDelete` alone does not protect the filesystem in code. Keep the `metadataServer` block identical to the MDS-only form so promotion does not roll the MDS.
- An EC default data pool is outside Rook's documented shape (the docs say EC data pools should be secondary) but passed the lab at 2+2. Expected diff: Rook creates an unreferenced `<pool>_ecprofile` erasure-code profile (compare its parameters with the live profile; a different one is a hold), re-issues `osd pool set <pool> allow_ec_overwrites true` every reconcile, and `osd pool create` on the existing pool returns "already exists" rc 0.
- Flux Kustomization for the CRs: `prune: false`, `deletionPolicy: Orphan`, `force: false`, `wait: false`, `dependsOn` the cluster Kustomization. Four independent brakes: `nodelete` on pools, `preserve*` fields, the per-object prune annotation, and the Kustomization settings.
- A size 2/min 1 pool stays at 2 permanently: size 1 blocks `ok-to-stop`. Rook forcing `mon_allow_pool_size_one=true` is then harmless.

Lab-proven behaviours: operator restart, a forced operator kill mid-reconcile and a mon pod delete cause no PG movement and no eviction; each operator restart bumps mon Deployment generation by 2 with no template change (gate on quorum and the mon record, not generation); after a cold boot the CephCluster shows Progressing for 3 to 5 minutes (gate on Ready with a wait).

### 6.3 CSI and monitoring

We kept the external ceph-csi throughout (existing users; clusterID = fsid and the mon list survive because mon IPs are preserved) and set `csi.skipUserCreation: true`. The CSI takeover (Rook's ceph-csi-operator replacing hand-applied ceph-csi) was rehearsed in the lab: no PV recreated, fuse mounts survived, a Velero restore worked, but the CSIDriver object is recreated by the operator even when pre-annotated (a window of about 1m40s with `attachRequired` true and zero VolumeAttachments), so do the Driver CRs first and node by node. [PLANNED] for production.

cephadm also ran Prometheus, Grafana and Alertmanager on one host. Before that host converted we pointed the dashboard at an existing external Prometheus (`dashboard.prometheusEndpoint`) and Alertmanager/Grafana (`cephConfig.mgr` entries for `mgr/dashboard/ALERTMANAGER_API_HOST` and `GRAFANA_API_URL`), so nothing depended on the cephadm stack when it disappeared. Note that the `PROMETHEUS_API_HOST` write crashed the dashboard and prometheus modules on a cephadm standby mgr that had a port clash ([P-20](pitfalls.md#p-20-mgr-dashboard-port-clashes)).

---

## 7. Phase 4: the last host and the cephadm decommission

### 7.1 The last host [PROD]

In our cluster one host carried the awkward services: the only remaining cephadm RGW, the haproxy/keepalived ingress holding a VIP, the only remaining SMB node, a cephadm mgr, the host whose `ceph` CLI had been the gate driver, the cephadm monitoring stack and a mon. It was converted last, after:

- RGW rebuilt on Rook and cut over, including the ingress and VIP ([rgw-rebuild.md](rgw-rebuild.md));
- the SMB service stopped (the outage was accepted; its replacement followed the next day, [smb-replacement.md](smb-replacement.md));
- a Rook mgr active, and the gate driver, evidence directory and baselines moved to the Rook toolbox;
- the dashboard's monitoring endpoints moved off the cephadm stack;
- the hand-maintained references swept;
- its controller model and boot recipe verified read-only.

Then the normal window. After it all seven mons were Rook pods (quorum 7/7, monmap epoch still 39).

### 7.2 Decommission [PROD], executed the day after the last host

Point of no return: until cephadm's state is removed, every step can be undone host by host. After it, only Rook-side rollbacks exist.

| Step | What we did |
|---|---|
| Preflight (read-only) | Proved no cephadm daemon is alive: `ceph mgr metadata`, `fs dump`, `service dump` for RGW, `osd metadata` hostnames and `node ls` show only Rook daemon names; `ceph orch status` already "No orchestrator configured". |
| Backup | `config-key dump mgr/cephadm/`, `mgr/smb/`, `smb/`; `config dump`; `mgr module ls`; full `auth export`. All of it is secret (SSH identity, CA keys, service passwords, cephx keys). We stored it as a Kubernetes Secret, never printed it, and kept nothing in the repository. |
| Disable modules | `ceph mgr module disable cephadm` and `ceph mgr module disable smb`. The active Rook mgr reloaded modules without a failover; every `CEPHADM_*` health check disappeared and the cluster went `HEALTH_OK` within 20 s. The prometheus module was unreachable for about 15 s. |
| config-keys | Deleted all 71 `mgr/cephadm/*` keys (host records, specs, inventory, certificate store, SSH identity, service passwords). Kept the SMB users key until the new SMB service had soaked. |
| Config rows | Removed `mgr/cephadm/*` rows, `osd_memory_target_autotune`, cephadm's service-level `mds.<fs> mds_join_fs` rows, a stale `mgr/dashboard/MULTICLUSTER_CONFIG` (it held an expired dashboard token pointing at a retired host) and the leftover `public_addr` host pins. |
| cephx entities | Removed 22 cephadm-style entities (`client.crash.<host>`, `client.ceph-exporter.<host>`, `mgr.<host>.<suffix>`, `mds.<fs>.<host>.<suffix>`). Kept the legacy RGW and SMB entities for their own clean-up steps. |
| Alerting | Expired every cephadm-related silence. |

Rollback, if ever needed: restore keys and entities from the backup Secret (`ceph config-key set`, `ceph auth import`), then `ceph mgr module enable cephadm` (the restored `pause` key brings it back paused).

### 7.3 Still to do [PLANNED]

1. Re-enable Rook mon health checks. First sweep terminal mon pods (`Completed` pods linger after every reboot, [P-15](pitfalls.md#p-15-rook-mon-health-check-evicts-a-mon-because-of-a-completed-pod)), confirm the mon record (each mon once, mapping = expected node per mon, no two mon pods of any phase on one node), then set `healthCheck.daemonHealth.mon.disabled: false`.
2. Remove the `osd_crush_update_on_start` guard (CR edit plus `ceph config rm global osd_crush_update_on_start`); otherwise new OSDs are not placed in CRUSH automatically.
3. Restore the measurement guards: `ceph balancer on`, `ceph osd pool unset noautoscale`, then re-baseline.
4. Remove the legacy RGW zone and its pools after the soak ([rgw-rebuild.md](rgw-rebuild.md#legacy-zone-removal-planned)); then the legacy RGW and SMB cephx entities and the `.smb` pool.
5. Reboot proof of one host with the full Rook stack, and a documented Talos upgrade procedure for storage nodes (`--preserve`, the storage schematic).
6. The cephx cipher migration (rotate older `aes` keys to `aes256k` where clients support it; until then the `AUTH_INSECURE_*` warnings are muted).

---

## 8. Gates and rollback, step by step

Rollbacks are decided by a person; automation holds and reports. "Hash gate" means the Appendix B set.

| Step | Gate before moving on | Rollback |
|---|---|---|
| 0 Baseline | Second baseline 15 minutes later compares clean; balancer off and `noautoscale` on; health = allowlist | Nothing changed (undo the two guards if you want) |
| 1.1 Operator | Operator 1/1, 0 restarts, no CephCluster, 0 error lines; pod-network reach of all mons on both ports; hash gate identical | Revert commit, delete HelmRelease, CRDs, namespace by hand |
| 1.2 Freeze cephadm | Canary service unmanaged with no daemon restarts, then all services unmanaged, zero restarts | `ceph orch apply -i <export> --dry-run`, then apply |
| 1.3 Mon renames | Per iteration: quorum complete, epoch +2, names and IPs as expected, hash gate identical, ceph-csi pod restarts unchanged | Stop; letter-named mons are valid under cephadm; reverse = another rename |
| 1.4 Seed | Applies to nothing in Ceph; hash gate identical; pod-network `ceph -s` equals the cluster's | Delete the Secret and ConfigMap |
| 1.5 CephCluster | Ready; 9 Pending pods; hash gate identical apart from the table in 4.6; operator quiet; no mon remove/failover | Operator to 0, revert, strip finalizers, `ceph orch set backend cephadm`, recreate `client.bootstrap-osd` (about 45 to 60 minutes) |
| 2 P0 | [host-conversion.md P0](host-conversion.md#p0-baselines-and-preflight-gates) | n/a |
| 2 P1 | `noout` visible, silences active, alerting green, writers at zero, write rate near zero | Resume everything, unset `noout` |
| 2 P3 | Within 60 s: OSDs down not out, quorum 6/7, no inactive PG beyond 15 s, filesystems have an active MDS | Start the cephadm target |
| 2 P5 | Boot disk found by WWN, old disk absent, OSD disks unchanged, PV UUIDs equal, no foreign signature on OSD disks | R-A: abort before applying config |
| 2 P6 | Node Ready, cold-boot proof passed, links exactly NIC plus VLAN, jumbo 8972 from a worker pod to both IPs, switch ARP MAC equals NIC MAC, no topology labels | R-B |
| 2 P7 | Prepare log lists exactly the host's ids with no destructive string; OSDs up within 10 minutes; keys, metadata, tree unchanged; mon rejoined with the same name, address and epoch; mons exactly a..g; hash gate identical | R-C |
| 2 P8 | One revision bump per OSD that needed it; no inactive PG over 30 s; hash gate identical; PGs clean; mask removed; soak green | New reverse window (R-D) |
| 3 MDS | [mds-adoption.md](mds-adoption.md) | same |
| 3 CR adoption | POOLRULE, hash gate, `nodelete` still on, size assert, mount sweep, CR change diff | Revert commit and re-apply; escape hatch only for emergencies |
| 3 RGW | [rgw-rebuild.md](rgw-rebuild.md) | Old zone and pools untouched; restart the legacy unit |
| 4 Decommission | Preflight proves no cephadm daemon alive; backup taken; Rook health block after each step | Restore from the backup Secret |

The hash gate must hold at every step. If a step is expected to change something (an auth entity, a config row, an epoch), the expectation is written down before the step and the gate takes it as an allowance. Anything else is a hold.

---

## 9. Pitfalls

Every problem we hit, with symptom, root cause, fix and prevention, is in **[pitfalls.md](pitfalls.md)**. The ones that would bite any cephadm-to-Rook migration:

- Mon renames stall on a busy cluster (paxos join drift, ignored joins, a CLI crash on bracketed addresses).
- Rook's mon health check evicts a mon because of a `Completed` pod on the same node.
- An MDS-only CephFilesystem with a `startupProbe` override fails to render.
- A cephadm CephFS standby takes the standby-replay slot when Rook enables it, so the handover order matters.
- Rook cannot serve a realm-less RGW zone, and it rewrites `pg_num` of the shared `.rgw.root` pool.
- Hand-written Endpoints and other references keep pointing at daemons that no longer exist.
- A GitOps controller undoes `kubectl`-level freezes of writers.

---

## Appendix A: the worked example

### A.1 The cluster

| Item | Value |
|---|---|
| Hosts | 7 (ceph-1 to ceph-7); Ceph network on the native VLAN, Kubernetes network on a tagged VLAN on the same switch port; hosts kept their Ceph IPs |
| Ceph | 20.2.4 Tentacle, cephadm with Docker, Ubuntu 24.04 hosts, 44 OSDs, 7 mons, 3 mgrs, 5 MDS, 3 RGW plus 2 haproxy/keepalived (VIP), 2 SMB (CTDB), Prometheus/Grafana/Alertmanager, exporters on every host |
| Pools | 18 pools and 1473 PGs at the start, all `nodelete`; hand-made rules `rep-ssd`, an EC rule on HDD, `rep-hdd`, `rep-nvme`; 234 upmap items; the EC 5+2 pool (`min_size 6`) is the CephFS default data pool. 25 pools and 1561 PGs after the RGW rebuild |
| CephFS | a large filesystem (62 TiB, 14.3 M inodes) and a smaller one (pool size 2/1, quota) |
| RGW | realm-less zone `default`, 12 users and 12 buckets, about 270 GiB, hand-written Endpoints for Kubernetes clients |
| Clients | 80 RBD PVs and 10 static CephFS PVs through external ceph-csi (some on ceph-fuse), hypervisor VMs on RBD, 3 non-Kubernetes kernel clients, Velero, database backups (CloudNativePG WAL archiving), SMB users |
| Kubernetes | Talos v1.14.2, 4 control planes and 3 workers before the first storage node; Flux, MetalLB, Traefik (Gateway API) |
| Hardware | 3x Dell R430 (HBA330 Mini, Intel 82599 10G, iDRAC8 on a shared LOM port), 4x Dell R730xd (HBA330 Mini or PERC H730 Mini in HBA mode, Mellanox ConnectX-4 Lx 25G, dedicated iDRAC8), about 63 GiB RAM per host; one Cisco Nexus 92160YC-X routing the storage and Kubernetes VLANs statelessly (VRF, MTU 9216) |
| Monmap epoch | 21 before phase 1, 38 after the renames, 39 after the first Rook reconcile, then unchanged through every window |

### A.2 Hosts

| Host | Model | OSDs | Controller | Boot disk change | Other cephadm daemons |
|---|---|---|---|---|---|
| ceph-3 | R430 | 5 (1 SSD, 4 HDD with DB LVs on an NVMe) | HBA330 | SMART-failed 256 GB SSD to a 120 GB enterprise SATA SSD (same bay) | mon, standby MDS |
| ceph-2 | R730xd | 6 (1 SSD, 1 NVMe, 4 HDD) | HBA330 | SMART-failed 256 GB SSD to a 400 GB SAS SSD | mon, RGW, ingress (backup), SMB node |
| ceph-6 | R730xd | 7 | HBA330 | 300 GB SSD to a 200 GB SAS SSD (EMC-branded) | mon |
| ceph-7 | R730xd | 10 (HDD, SAS SSD, NVMe) | PERC H730 Mini in HBA mode | two 300 GB SSDs in software RAID1 to one 200 GB SAS SSD (EMC-branded) | mon, standby MDS, RGW |
| ceph-4 | R430 | 5 | HBA330 | SMART-failed 256 GB SSD to a 120 GB SATA SSD | mon, active mgr, then the only active MDS of the large filesystem |
| ceph-5 | R430 | 5 (an OSD SSD sits in the bay next to the boot disk) | HBA330 | 240 GB SSD (85% worn) to a 120 GB SATA SSD | mon, active mgr |
| ceph-1 | R730xd | 6 | PERC H730 Mini in HBA mode | 300 GB SSD to a 400 GB SAS SSD | mon, mgr, the only RGW, ingress/VIP, the only SMB node, monitoring stack, CLI driver |

Order of events: phase 1 over two days; ceph-3 and ceph-2 on day 2; ceph-6 on day 3; ceph-7, the MDS handover and ceph-4 on day 4; ceph-5, the RGW rebuild and ceph-1 on day 5; decommission and SMB replacement on day 6.

### A.3 Window timings

T0 is the stop of the cephadm target on the host. Times are from the window logs.

| Step | ceph-3 | ceph-2 | ceph-6 | ceph-7 | ceph-4 | ceph-5 | ceph-1 |
|---|---|---|---|---|---|---|---|
| T0 to node Ready (cold-boot proven) | 3 h 18 min | 26 min | 37 min | 31 min | 34 min | 31 min | 2 h 01 min * |
| T0 to all OSDs up | about 3 h 25 min | about 32 min | about 42 min | about 35 min | about 40 min | about 39 min | 2 h 10 min * |
| `noout` duration | 3 h 33 min | about 41 min | about 50 min | about 41 min | about 51 min | about 53 min | 2 h 18 min * |
| Heavy writers off | 3 h 48 min | about 47 min | about 58 min | about 43 min | about 113 min (ARP incident) | about 55 min | 2 h 20 min * |
| OSD Deployments after the prepare job | 150 s | 75 s | | 35 s | 10 s | | under 2 min |

\* ceph-1 includes about 1 h 45 min with the host powered off waiting for a person at the rack. From the Talos maintenance API answering to all OSDs up took 19 minutes.

Planned per host: down 55 to 60 minutes, `noout` about 75 minutes, whole session 3.5 to 4 hours. The first window's overrun was entirely boot media and firmware discovery. From the second host on the targets were met, except the ARP incident on the fifth and the wait on the seventh.

The placement hashes (placement map, CRUSH, rules, pools, tree) were identical to the pre-window baseline at the adoption gate and the ROOK-2 gate of every window, and each host's baseline equalled the end state of the previous window (the RGW rebuild between the sixth and seventh host changed the pool set as expected, so the seventh window used a fresh baseline).

---

## Appendix B: gate toolkit

The gate is a script (Python, run where `ceph`, `python3` and `osdmaptool` are available: a `cephadm shell` before Rook, the Rook toolbox afterwards) that writes a summary JSON of hashes and names per label, compares it with a base summary, and exits non-zero on any unexplained difference. It never writes keys. A generalized version is in [examples/scripts/zero-movement-gate.py](../examples/scripts/zero-movement-gate.py).

### B.1 The hashes

```bash
# placement hash: raw/up/acting of every PG
ceph osd getmap -o /tmp/om.bin
osdmaptool /tmp/om.bin --test-map-pgs-dump-all \
  | grep -E '^[0-9]+\.[0-9a-f]+ raw' | sort | sha256sum | cut -c1-16
# NOT --test-map-pgs-dump (acting only; hidden by pg_temp). Includes OSD up state: compare only when all OSDs are up.

ceph osd crush dump        | sha256sum | cut -c1-16     # crush hash
ceph osd crush rule dump   | sha256sum | cut -c1-16     # rules hash
ceph osd tree              | sha256sum | cut -c1-16     # tree hash (contains up/down status)
# pools hash: stable fields only (id, size, min_size, crush_rule, pg_num, pg_placement_num, type, erasure_code_profile,
# quota_max_bytes/objects, flags_names, options, application_metadata) from `ceph osd dump -f json`
ceph auth get-key mon. | sha256sum | cut -c1-16         # mon. key hash
# auth: per entity sha256(key):sha256(caps) from `ceph auth ls -f json`; never print keys
# per OSD: ceph auth get-key osd.N | sha256sum | cut -c1-8
```

Plus: monmap epoch and per-mon address vector (two addresses each), the cipher fields from `ceph mon dump -f json`, OSD flags and counts and blocklist count, `ceph versions`, mgr active/standby/modules, `fs dump` (MDS states and fscids), the config dump keyed `section|mask|name` (hash the values of secret-looking names, including any value that looks like a JWT), orch status/services/ps (unavailable after phase 1).

### B.2 Gate flags that mirror expected differences

`--quick`, `--allow-health A,B`, `--renamed <letters>`, `--epoch-delta K`, `--allow-auth-add/-del`, `--allow-cfg '<regex on section|mask|name>'`, `--allow-cipher-change`, `--allow-mgr-change`, `--orch-gone`, `--allow-unmanaged`, `--allow-blocklist`, `--base <alternate baseline>`. Exit 1 on any FAIL means HOLD. Differences we saw at every window that are not movement: config rows for host-pinned `public_addr` and the removed memory mask, MDS states after a cephadm standby leaves, mgr set changes, the health allowlist (`OSD_FLAGS` during the window; growing `CEPHADM_*` counts), blocklist count changes as entries expire. Our first script printed several of these as FAIL lines; the example version handles them.

### B.3 Host and Kubernetes checks per gate

- Per cephadm host: failed ceph units, container names with start time and restart count, `removed/mon.*` directories, free space under `/var/lib/ceph`. A diff against the baseline must show no daemon restarted.
- Kubernetes: nodes, non-Running pods, ceph-csi restart counts, CSIDriver UIDs, the CSI config mon lists, Rook pods, CRDs, and per-node kernel log counts of `libceph` and `blocklist|bad authorizer|auth method|hung task`.

### B.4 Zero-movement discipline

Balancer off and `noautoscale` set before the baseline; the baseline and the later gate compare the same fields; a host's pre-window baseline is compared with the previous host's final state; in a window, OSD-dependent hashes are only meaningful when every OSD is up (a state with OSDs down is expected to differ).

---

## Appendix C: Rook documentation references, field names and prior art

Pin the documentation path to your Rook version (here v1.21) before quoting; summaries of the docs are not a substitute for the CRD YAML of your exact release (`deploy/examples/crds.yaml`).

| Topic | URL |
|---|---|
| CephCluster CRD | https://rook.io/docs/rook/v1.21/CRDs/Cluster/ceph-cluster-crd/ |
| Host storage cluster (node names equal `kubernetes.io/hostname`) | https://rook.io/docs/rook/v1.21/CRDs/Cluster/host-cluster/ |
| CephBlockPool | https://rook.io/docs/rook/v1.21/CRDs/Block-Storage/ceph-block-pool-crd/ |
| CephFilesystem | https://rook.io/docs/rook/v1.21/CRDs/Shared-Filesystem/ceph-filesystem-crd/ |
| CephObjectStore | https://rook.io/docs/rook/v1.21/CRDs/Object-Storage/ceph-object-store-crd/ |
| Disaster recovery (restoring mon quorum, adopting a cluster, namespace-deleted restore) | https://rook.io/docs/rook/v1.21/Troubleshooting/disaster-recovery/ |
| Mon health | https://rook.io/docs/rook/v1.21/Storage-Configuration/Advanced/ceph-mon-health/ |
| Ceph CSI drivers | https://rook.io/docs/rook/v1.21/Storage-Configuration/Ceph-CSI/ceph-csi-drivers/ |
| Rook v1.21.0 release | https://github.com/rook/rook/releases (tag `v1.21.0`) |

Field names used in this guide, with notes where the public page and the source disagree:

- CephCluster: `cephVersion.image`, `dataDirHostPath`, `skipUpgradeChecks`, `network.provider`, `network.addressRanges.public`, `mon.count`, `mon.allowMultiplePerNode`, `mgr.count`, `mgr.modules[]`, `dashboard.{enabled,ssl,port,prometheusEndpoint,prometheusEndpointSSLVerify}` (the last two are in the v1.21.0 source and chart CRD, not on the page we fetched), `healthCheck.daemonHealth.mon.{disabled,interval,timeout}`, `healthCheck.livenessProbe.{mon,mgr,osd}`, `healthCheck.startupProbe.{mon,mgr,osd}`, `cephConfig.<section>.<key>`, `csi.skipUserCreation` (source and lab; not on the page), `placement.{mon,mgr,osd,prepareosd,cleanup,all}`, `storage.{useAllNodes,useAllDevices,allowDeviceClassUpdate,allowOsdCrushWeightUpdate,nodes[].{name,resources}}`, `removeOSDsIfOutAndSafeToRemove` (check its path in your CRD), `cleanupPolicy.confirmation`, `disruptionManagement.managePodBudgets`, `resources.<daemon>`.
- CephBlockPool: `spec.name` (only `.nfs`, `.mgr`, `.nvmeof`, `.rgw.root`), `failureDomain`, `deviceClass`, `replicated.{size,requireSafeReplicaSize}`, `erasureCoded.{dataChunks,codingChunks}`, `enableCrushUpdates`, `enableRBDStats`, `parameters`, `quotas`.
- CephFilesystem: `metadataPool`, `dataPools[]`, `preservePoolNames`, `preserveFilesystemOnDelete`, `preservePoolsOnDelete` (deprecated), `metadataServer.{activeCount,activeStandby,resources,placement,priorityClassName,livenessProbe,startupProbe}`. The page does not document existing filesystems or the MDS-only form.
- CephObjectStore: `metadataPool`, `dataPool`, `preservePoolsOnDelete`, `defaultRealm`, `gateway.{port,securePort,instances,sslCertificateRef,hostNetwork,resources,placement}`, `hosting.{dnsNames,advertiseEndpoint.{dnsName,port,useTls}}`, `healthCheck.{startupProbe,readinessProbe}`. The page is silent about realm-less zones; `sharedPools` exists in source.
- Seed objects: Secret `rook-ceph-mon` keys `fsid`, `mon-secret`, `ceph-username`, `ceph-secret`; ConfigMap `rook-ceph-mon-endpoints` keys `data`, `mapping`, `maxMonId` (the DR page lists `csi-cluster-config-json` too, regenerated by Rook).

Prior art and public positions:

| Source | Position |
|---|---|
| rook/rook issue 278 "Takeover existing cluster" (2016, closed 2017) | Not planned. |
| rook/rook PR 8574 "[WIP] Merging native ceph clusters into rook" (2021, closed stale) | Not supported; mon IPs must not change in Rook. |
| rook/rook discussion 12045 "Baremetal migration" (2023) | Too complex; use external mode or a fresh cluster. |
| rook/rook discussion 13792 "migrating existing ceph clusters to rook" (2024) | Maintainers cannot validate a path; community docs welcome. |
| rook/rook issue 14971 (2024) | Long requested; maintainers cannot test; nothing added. |
| rook/rook discussion 12841 (2023) | "If the pool already exists, Rook will just start managing it" for a CephBlockPool. |
| rook/rook PR 15056 (merged 2024-12) | Named MDS metadata pools; why `preservePoolNames` works. |
| rook/rook issue 17477 (2026) | A report of Rook adopting an LVM OSD created by `ceph-volume` on Talos. |
| rook/rook issue 14505 | SMB support, closed as not planned. |
| Rookify, Sovereign Cloud Stack (GitHub SovereignCloudStack/rookify) | Python migration tool for ceph-ansible clusters; archived; moves daemons rather than reinstalling in place. |
| openstack-helm "Migrating Ceph to Rook" | Disruptive, "risky even with testing", swaps the fsid; same shape as Rook's own DR adoption. |
| Ceph `cephadm adopt` docs | The reverse direction; mature; same LV-tag contract. |

Searches in October 2026 found no end-to-end public report of an in-place cephadm to Rook migration. Slack, Reddit and mailing-list archives were not searched thoroughly, so "none found" is not "none exist".

---

## Appendix D: evidence status and open items

### D.1 Executed versus planned

| Item | State |
|---|---|
| Lab rehearsal, runs 1 to 8 (4 hosts), device-class gate, escape hatch, adoption of 19 pools and 2 filesystems as CRs, CSI takeover | [LAB] |
| Phase 1 (operator, 7 mon renames, seed, CephCluster, cephadm pause) | [PROD] |
| Host windows, 7 of 7 | [PROD] |
| Mon placement pin | [PROD] |
| MDS-only CRs, handover, failover drills, GitOps sentinel | [PROD] |
| RGW rebuild, copy and client cutover | [PROD] |
| SMB replacement | [PROD] |
| cephadm decommission (modules, config-keys, config rows, cephx entities) | [PROD] |
| Deliberate mgr takeover sequence | [LAB] only (production moved the mgr by circumstance, 6.1) |
| Adopted pool and filesystem CRs beyond MDS-only, CSI takeover | [LAB] / [PLANNED] |
| Legacy RGW zone and pool removal, mon health checks, `osd_crush_update_on_start` guard removal, balancer and autoscaler back on, cipher migration | [PLANNED] |
| Rollbacks in production | None needed. Lab per-host rollback: 2 min 24 s on VMs. |

### D.2 What is unproven

- Mon rename stalls: the root cause of the ignored joins is unproven; the drift fix is understood.
- The failure-margin story depends on EC 5+2 on seven hosts surviving a roll by Rook; not exercised (we never rolled two hosts at once).
- Day-2 OSD replacement, and adding a new OSD on an NVMe that already carries DB LVs, are source reading only (rook#7121 and #13240 describe the limitation).
- Bad cases for the mon seed (mapping Address not on the node, hostname label mismatch, a seed lacking the live quorum) were never run. Never start a Rook mon whose `mon_host` lacks the live quorum: a mon with a one-entry monmap wins a standalone election and becomes a second cluster with the same fsid.
- Several negative lab tests were never run (guard-off CRUSH move, alternative mon strategies, a caps-mismatch OSD).
- No Ceph 20.2.4 lab run (the lab was 20.2.3).
- The soak after each window was short; first-night behaviour was watched but is thinly evidenced.

### D.3 Strategies we did not take

Rook's mon "externalMonIDs" strategy (experimental) and the disaster-recovery transplant (stop all mons, rewrite the monmap to a single mon, grow) were analysed but not run. The rename-and-seed strategy was chosen because it needs no mon downtime and no client configuration change.
