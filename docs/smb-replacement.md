# SMB: replacing the cephadm SMB service with a single Samba pod

Rook has no SMB support and none planned (rook#14505, closed as not planned). The Ceph `smb` mgr module requires cephadm orchestration. So the cephadm `smb` service (two CTDB nodes on two Ceph hosts) could not be handed over; it had to be replaced. [PROD]

What we ended up with is much simpler than what we first planned. The first plan tried to keep the SMB service up across the conversions (same configuration objects, same machine SID, a rollback to cephadm). In the end we accepted an outage: the cephadm SMB service went down with the last host's conversion and its replacement went live the next day. If your SMB users can live with a short outage, take the simple path below.

## What the research found (and why each point mattered)

These came from reading the `smb` mgr module, sambacc and the live cluster. They drove the first, zero-downtime design and still explain the simple one.

1. **Users and join credentials are not in the `.smb` pool.** `.smb` holds `config.smb`, `cluster-info`, `spec.smb` and CTDB objects; the users JSON and join JSON live in the **mon config-key store** under `smb/config/<cluster_id>/`. A sambacc pod that reads the RADOS config must list the users URI too, or it has no users.
2. **There is no single stable cephx entity:** cephadm mints `client.smb.config.<cluster>.<rank>.<gen>.<host>.<suffix>` per daemon per redeploy. Give the new pod its own entity.
3. **`config.smb` is rendered for CTDB** (`instance_features: ["ctdb"]`); a non-clustered pod reading it would wait for a ctdbd that does not exist. Reading it needs a `config:merge` overlay. Never rewrite `config.smb` itself while a cephadm rollback depends on it.
4. **`emptyDir` for `/var/lib/samba` is wrong:** the standalone machine SID lives in `secrets.tdb` (under cephadm, inside CTDB persistent storage). An emptyDir mints a new SID per restart. Use a PVC. Samba keys the standalone SID on the `netbios name`; never rename it.
5. **A `ceph smb` command on a Rook mgr could delete production config** [inferred from source, high impact]: in `_sync_clusters`, an empty modified set takes the all-clusters branch and computes `removed = ext_ids - set(cluster_ids)`; if the module's internal store was not carried over it would remove the users JSON and `config.smb`/`spec.smb`. Freeze all `ceph smb` writes, back up first, and use `--password-filter hidden` (`ceph smb show` prints plaintext passwords by default). We disabled the `smb` module at decommission.
6. **Do not copy CTDB.** Its public IPs need `NET_ADMIN`/`NET_RAW` and L2 control and do not map onto a Kubernetes LoadBalancer; nothing under Rook maintains the cluster meta object. A single replica means every pod restart is a full SMB outage; we accept that.
7. **The SMB address was shared with the RGW ingress VIP**, which worked only because keepalived placed the VIP on the host where smbd listened. Removing the RGW ingress killed SMB on that address even while the cephadm smbd still ran. Plan the RGW and SMB cutovers together.
8. **Pin the Samba image by digest and mirror it.** A mixed pair of Samba builds crash-loops with `NT_STATUS_SXS_VERSION_CONFLICT`. The upstream `samba-server` images on quay.io for the Ceph 20 flavour are rebuilt daily and superseded manifests disappear within one or two days: the build we had validated could no longer be pulled when we needed it. Mirror the digest you test into your own registry (for example with `skopeo copy --preserve-digests`) before you need it.
9. **Probes must list the shares.** A TCP probe on 445 passes while smbd has loaded zero shares (a known race in the cephadm sidecar setup). Use `smbclient -L` and assert every share name.

## The design as built

| Item | Choice | Why |
|---|---|---|
| Workload | `Deployment`, 1 replica, `strategy: Recreate`, one container | Non-clustered smbd instances share no lock state, so never two. Recreate avoids the RWO PVC deadlock. |
| Process | `samba-container run smbd`: sambacc's default init (registry import, users with pinned uid/gid, passdb import), then smbd in the foreground | No ctdbd, configwatch, proxy, metrics or winbind sidecars. The memory leak, the empty-share race and the false EACCES we had seen under cephadm all came from those. |
| Config source | sambacc JSON in a hash-suffixed ConfigMap in Git, users JSON in a Secret | Not the RADOS `.smb` objects: no `.smb` pool dependency (Rook cannot manage it: CephBlockPool rejects dot-names other than `.mgr`, `.nfs`, `.rgw.root`, `.nvmeof`), no `config-key get` cap, no overlay, the `smb` mgr module can be retired, and share changes are reviewed in a pull request. Cost: share and user changes restart the pod (about a minute). |
| VFS | `ceph_snapshots ceph_new` on every share, `ceph_new:proxy = no` | Same provider on every share; the proxy mode produced false EACCES errors for Windows clients. |
| ACLs | `acl_xattr` dropped. Who may connect: share-level `valid users`. What they may do inside: POSIX uid/gid/mode | See below. |
| Shares | Same CephFS subvolume paths as the cephadm `config.smb` | Data untouched; a share for a retired use case was dropped. |
| Users | Two users with **explicitly pinned** uid and gid (1000:1000 and 1001:1001), same passwords as before | Numeric owners on CephFS stay valid. sambacc had derived uid **and** gid as 1000 + index, so the second user's group was 1001, not the 1000 we had assumed. |
| cephx | A dedicated client with CephFS caps only | The pod no longer reads RADOS objects or config-keys. |
| State | PVC (Ceph RBD, 1 GiB) on `/var/lib/samba` | Keeps the machine SID, RIDs, passdb and registry stable across restarts. |
| Address | Its own LoadBalancer IP, port 445, `externalTrafficPolicy: Local` | No sharing with RGW. Local: one replica, no SNAT, real client IPs in `smbstatus`. |
| Image | The upstream build mirrored by digest | See finding 8. |
| Resources | requests 250m / 512Mi, memory limit 4Gi | One libcephfs per smbd child per filesystem. Revisit with data. |
| Probes | startup and readiness list all shares with `smbclient -L`; liveness TCP 445, lenient | A restart is an outage, so liveness is slow to fire. |

The cephx client:

```bash
ceph auth get-or-create client.smb-k8s \
  mon 'allow r fsname=<fs-a>, allow r fsname=<fs-b>' \
  mds 'allow rw fsname=<fs-a>, allow rw fsname=<fs-b>' \
  osd 'allow rw tag cephfs data=<fs-a>, allow rw tag cephfs data=<fs-b>' > /dev/null   # the output contains the key
```

A path-restricted MDS cap would be tighter, but `vfs_ceph_new` mounts the filesystem root and a path-restricted client may be refused that mount. Untested; a follow-up.

The Secret was filled by a small script that read the cephx key and the existing user passwords (from the config-key `smb/config/<cluster>/users-groups.0.json`) and wrote them into the manifest without printing anything.

### Why `acl_xattr` was dropped

The cephadm service stored Windows ACLs in an xattr (`user.NTACL` in our configuration, not the module default `security.NTACL`). Those ACLs name SIDs built on the old machine SID, which lived only in CTDB persistent state on the two cephadm hosts. That state was gone with the hosts and the SID had never been captured. A new pod mints a new SID, so with `acl_xattr` kept, every SMB-created file would present an ACL naming SIDs nobody has; users would match only the `Everyone` entry. Without `acl_xattr`, Samba ignores those blobs and derives access from POSIX owner, group and mode, which already governed everything non-SMB clients wrote. Trade-off: editing permissions from the Windows Security tab no longer works.

If you want to re-enable it later, clear the old blobs first so they cannot apply under the new SID (`find <share> -exec setfattr -x user.NTACL {} +` from something that mounts the filesystem), then add `acl_xattr` with a fresh attribute name. Capturing the old SID before the cephadm hosts go away (`net getlocalsid` on a cephadm smbd) and pinning it with `net setlocalsid` would have avoided this; we did not do it.

## Cutover and acceptance [PROD]

1. Mirror the image; create the cephx client; fill the Secret; merge; the GitOps controller applies it.
2. Pod Running and Ready; `smbd -V` and the libcephfs version checked against the cluster (Samba 4.24.8, libcephfs 20.2.4 against Ceph 20.2.4); all shares listed; `pdbedit -L` and `/etc/passwd` show the pinned uid/gid; DNS name registered for the new LB IP.
3. A Windows client mapped the shares with the existing passwords (typed by the user).
4. A round-trip copy of a test file on every share, hashes compared; ownership checked from a pod that mounts the same subvolumes (uid as pinned; the group followed setgid parent directories, as expected); test files removed.
5. The probe in the external monitoring switched to the new name.

Total SMB outage: from the last cephadm host's conversion in the evening to the next afternoon.

## Clean-up

Kept for a week after cutover, then planned for removal: the `.smb` pool (it has `nodelete`), the `smb/config/*` config-keys, the two cephadm per-daemon `client.smb.config.*` entities and the old filesystem client entity. The `smb` mgr module was disabled at decommission.
