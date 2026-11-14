# Debian deploy project

This is the existing two-stage Debian boot-stick and Wayland/Hyprland project.
`xcc deploy` opens this checkout; `/opt/xcloud/repos/deploy` is only a pointer.

## Scope and source

- Read `README.md`, `docs/deployment.md`, `setup/README.md` and the current Git
  diff before changing code. Preserve uncommitted work, including `FIRST-RUN.sh`.
- Keep the two-stage boundary: `iso/` installs the Debian base only; `setup/` and
  the Thor-specific `FIRST-RUN.sh` run after first boot. Do not move desktop,
  driver, xccode or credential setup into the installer.
- Use `iso/hosts.json` for existing host profiles and storage guards. Confirm the
  requested target before changing hardware policy; this Arch workstation is
  not a Debian post-install test target.
- Source xCloud requirements from `/opt/xcloud/repos/xcloud-docs`. Historical
  evidence is in `/opt/xcloud/repos/xcloud-state/agents/open-loki-agent/` and
  `/opt/xcloud/repos/loki-handover/BASE-INSTALL-READY.md`; do not overwrite another
  agent's records or treat old artifacts as proof for changed source.

## Safety and verification

- Run `make check` for shell lint, JSON validation and unprivileged regression
  tests. These checks are not VM installation or physical driver acceptance.
- Never execute host bootstrap, install packages, contact remote hosts, write USB
  media or change disks without explicit approval for the operation and target.
  Never attach host disks or writable physical media to installer test VMs.
- Keep release builds tied to a clean reviewed commit. Do not bypass dirty-tree,
  signature, checksum, disk-identity, erase-consent or post-install readiness gates.
- Keep credentials, private artifacts and machine secrets out of source and
  images. Verify upstream versions and integrity before selecting dependencies.
- Delegate independent work only when authorized, with clear file ownership.
  Do not commit, push or publish without approval. Report exact checks and
  remaining blockers; do not claim an updated USB without media verification.
