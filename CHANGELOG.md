# Changelog

All notable changes to this write-up are recorded here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and published states are tagged releases ([Semantic Versioning](https://semver.org/spec/v2.0.0.html) in spirit: major for a restructure, minor for new sections or completed migration steps, patch for corrections).

## [Unreleased]

Planned, as the remaining steps are done in production:

- Legacy RGW zone and pool removal (planned after a 7-day soak from the S3 cutover).
- Re-enabling Rook's mon health checks, removing the `osd_crush_update_on_start` guard, turning the balancer and autoscaler back on.

## [1.0.1] - 2026-10-24

### Changed

- Benchmark figures removed from the README and `docs/after-the-migration.md`. The performance section now gives the method and the findings. A re-run the same afternoon from the fastest clients, with more parallelism, repeated the single-job results and confirmed the conclusion: no regression.

### Fixed

- The 1.0.0 LAN S3 result was blamed only on the 1G LoadBalancer announcer; the test workstation was also behind a 1G switch uplink (`docs/after-the-migration.md`, `docs/pitfalls.md` P-23).

## [1.0.0] - 2026-10-24

### Added

- README with results, status, prerequisites and the overall flow.
- `docs/guide.md`: invariants, feasibility checklist, lab rehearsal, Phase 1 in full (operator, cephadm freeze, mon renames with the stalls we hit, identity seed, CephCluster, first-reconcile effects), phase summaries, decommission, gates and rollbacks, references and evidence status.
- `docs/host-conversion.md`: the per-host window (P0 to P8), the OSD and mon adoption mechanism, gates, rollbacks and measured timings for all seven hosts.
- `docs/mds-adoption.md`: MDS-only CephFilesystem handover and the standby-replay ordering trap, with measured stalls.
- `docs/rgw-rebuild.md`: why the realm-less zone was rebuilt, the store as built, the copy window and its deviations, dashboard RGW key rotation under Rook, the planned legacy zone removal.
- `docs/smb-replacement.md`: replacing the cephadm SMB service with a single sambacc pod.
- `docs/dell-13g-talos-boot.md`, `docs/flux-freeze.md`, `docs/pitfalls.md` (P-1 to P-23), `docs/lessons-learned.md`.
- `docs/after-the-migration.md`: health and performance after the migration, the S3 LoadBalancer fix, the dynamic CephFS PVC node-key cap (applied to the live key and verified), the dashboard with an external Prometheus.
- `examples/`: CephCluster, seed objects, MDS-only and adopted CephFilesystem, CephObjectStore and its Service, Talos storage-node patch, Flux Kustomizations and operator HelmRelease, the zero-movement gate script, RGW copy helpers.
- Licences: CC BY 4.0 for documentation, MIT for scripts and examples.

[Unreleased]: https://github.com/Burks-Tech/cephadm-to-rook-in-place/compare/v1.0.1...HEAD
[1.0.1]: https://github.com/Burks-Tech/cephadm-to-rook-in-place/releases/tag/v1.0.1
[1.0.0]: https://github.com/Burks-Tech/cephadm-to-rook-in-place/releases/tag/v1.0.0
