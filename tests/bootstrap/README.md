# Stage 2 tests

Run from the repository root, as the normal user:

```sh
bash -n setup/bootstrap.sh setup/link-config.sh setup/session.sh
shellcheck setup/bootstrap.sh setup/link-config.sh setup/session.sh config/desktop/uwsm/env tests/bootstrap/container-smoke.sh
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests/bootstrap -v
```

The Python suite uses temporary fixtures (preferring `/tmp/opencode`) and sources
functions without running their host entrypoint. Root-required operations are
mocked. Tests cover argument/user/suite validation, read-only stock networking,
rejection of unsafe custom networking, scoped package plans, repository mixing,
dry-run nonmutation, fatal apt errors, exact service-policy restoration including
symlinks, atomic system-file preservation, user-config backup/idempotence, and
refusal of symlinked home-config parents. No host package manager, service, user,
desktop or configuration is changed.

## Optional real-package smoke check

Requires **rootless** Podman and an already cached, digest-pinned Debian base
image. This test refuses host root, mutable image tags and unknown profiles. It
does not pull images. Supply a trixie image for `generic`, or a forky image for
`loki`; profile guards still apply.

```sh
bash tests/bootstrap/container-smoke.sh DEBIAN_IMAGE@sha256:DIGEST generic
bash tests/bootstrap/container-smoke.sh DEBIAN_IMAGE@sha256:DIGEST loki
```

Only the source repository is mounted, **read-only**. No devices, system bus,
home directory or other host paths are exposed. The disposable container is
limited to 2 GB RAM, two CPUs and 512 PIDs. Signed apt downloads require network
access and container disk space. It first adds the init/base commands that OCI
slim images omit but d-i installs, then runs a real dry-run and two installations.
Source is copied to an immutable container-local test snapshot before execution,
so concurrent repository edits cannot invalidate Bash's open-script read offset.
`SYS_NICE` is included in the container's capability bounding set so Debian's
file-capability-bearing Hyprland executable can run its parser. This remains a
rootless user namespace with `no-new-privileges`, not host scheduling privilege.

Assertions: Hyprland `--verify-config` succeeds as an unprivileged user; package
operations succeed; original config and site policy survive; rerunning creates
no second user-config backup; ifupdown is masked and SDDM/NM/Bluetooth are
enabled offline; Debian polkit/PAM paths exist. For stable, PipeWire is not a
backport and no kernel image was pulled in. Neither profile pulls the full Plasma
workspace for its greeter theme. The last line must be:

```text
CONTAINER SMOKE OK (package/config checks only; no graphical session was started)
```

The container has no running systemd/login/Wayland session. It cannot test PAM
login, XDG autostart execution, rendering, lock authentication, actual session
cleanup, portal interaction, reconnection after reboot, suspend or hardware.
Those are integration / QEMU / physical-machine tests, not inferred successes.

The exact image digests, package versions and observed results for this change
belong in the implementation handoff. A source Git commit alone is not an apt
package lock; installed target versions are captured by bootstrap itself.

## Observed validation (2026-11-07)

The deterministic suite completed **26 tests, OK**. `bash -n`, ShellCheck and
`git diff --check` completed without diagnostics for the owned files.

Disposable amd64 trixie and forky containers installed the signed package core,
including SDDM's Maui/Xorg backend, and reported `config ok` for Hyprland
`0.55.2+ds-1~bpo13+1` and `0.57.0+ds-1`, respectively. Both reached bootstrap's
completion message, but the overall smoke runs **failed (exit 2)** because the
live bind-mounted script was edited during execution, invalidating Bash's read
offset after `main` returned. The test snapshot fix above is not yet rerun: work
stopped at the user's request. Full two-run smoke acceptance is **not claimed**.
No host/VM installation or graphical session was performed.
