# Examples

Sanitized versions of what we ran. Placeholders are written `<like-this>`; addresses use documentation ranges (`192.0.2.0/24` for the Ceph network, `198.51.100.0/24` for the Kubernetes network); domains use `example.com`. Read the comments: most of the value is in why a field is set the way it is.

| Path | What | Evidence |
|---|---|---|
| `cephcluster/cephcluster.yml` | CephCluster for the adoption, with the `storage.nodes` adoption shape and every guard | [PROD] |
| `seed/` | The `rook-ceph-mon-endpoints` ConfigMap and how to create the `rook-ceph-mon` Secret without exposing keys | [PROD] |
| `filesystem/cephfilesystem-mds-only.yml` | MDS-only CephFilesystem for an existing CephFS | [PROD] |
| `filesystem/cephfilesystem-adopted-LAB-ONLY.yml` | Fully adopted CephFilesystem (pools under Rook) | [LAB] |
| `objectstore/` | New single-site CephObjectStore next to a legacy realm-less zone, and its LoadBalancer Service | [PROD] |
| `talos/storage-node-patch.yml` | Talos patch for a storage host (VLAN layout, ARP sysctls, labels and taint, install by WWN) | [PROD] |
| `flux/` | Rook operator HelmRelease and Kustomizations with the never-prune brakes | [PROD] |
| `scripts/zero-movement-gate.py` | The hash gate | generalized from [PROD] |
| `rgw-copy/` | RGW snapshot/diff, user copy and bucket copy job | generalized from [PROD] |

Versions these were used with: Rook v1.21.0, Ceph 20.2.4, Talos v1.14.2, Kubernetes v1.36, Flux v2. Check every field against the CRDs of your exact Rook release before applying anything.
