# Stage 1: reviewed netinst repack

Run `sh iso/build.sh --help` for the release and private VM-test modes. The
upstream release input is pinned in `upstream.json` and verified before repacking.
This code does not write a disk or alter a running host.

The custom initrd is `/xc0/initrd.gz`, separate from Debian's original installer
and rescue initrds. The preseed callbacks gate exact target identity and local
consent. Since Debian's wrappers can swallow a failed callback, the config has
inline non-returning fallbacks and a separate executable partman init guard.

`late.sh` finishes the base and its requested suite transition. Its marker says
`stage=base`, not "desktop complete". Git configuration scripts are deliberately
run after installation and first boot; `/etc/xc0-next-steps` records the reviewed
commit and commands. No moving GitHub key feed or production NOPASSWD rule is installed.

See `../docs/deployment.md` before building or booting physical release media.
