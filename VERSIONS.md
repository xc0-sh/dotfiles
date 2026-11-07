# Reviewed inputs

No `latest` image tags or moving Git branches are used as executable install inputs.
APT packages come from Debian-signed repositories; each installed machine records
its actual package versions in the bootstrap log/evidence rather than treating
today's testing package list as a permanent pin.

| Component | Version | Source | Checked | Integrity / evidence |
|---|---|---|---|---|
| Debian amd64 netinst | 13.7.0 | `cdimage.debian.org/debian-cd/current/amd64/iso-cd/debian-13.7.0-amd64-netinst.iso` | 2026-11-07 | SHA512 `ef04d0276850d70e4aaec0b55003628660e9631b26e35215a70ab3c8c707294a4b12731bc73c75a932902f703a7c278310ca99df28d6b36fb06d5618d8d0f678`; checksum file verified using Debian key `DF9B9C49EAA9298432589D76DA87E80D6294BE9B`, fingerprint from Debian's CD verification page |
| Ventoy (reviewed release; deployment pending) | 1.1.18 | [Official latest release API](https://api.github.com/repos/ventoy/Ventoy/releases/latest), [v1.1.18 release](https://github.com/ventoy/Ventoy/releases/tag/v1.1.18) | 2026-11-07 | Latest API reports `v1.1.18`, `draft=false`, `prerelease=false`; upstream release body and asset digest publish SHA256 `d86ff9de63d94c8b8f1f6b14d23c55af4b88e8207bf2893890892a6a97b1ad54` for `ventoy-1.1.18-linux.tar.gz`. Archive downloaded and locally verified against that checksum on 2026-11-08. USB installation remains pending; actual USB version not yet measured. |
| Post-install configuration source | full reviewed commit SHA recorded in each ISO manifest | `https://github.com/xc0-sh/dotfiles.git` | at ISO build | After first boot, check out the exact 40-character SHA from `/etc/xc0-next-steps`; the installer itself does not fetch or execute configuration scripts |
| Desktop runtime packages | selected at provisioning | Debian trixie + targeted backports, or Debian testing | at provisioning | Debian archive signature verification stays enabled; installed package list recorded on the target |
| `actions/checkout` | v7.0.1 | GitHub actions/checkout | 2026-11-07 | commit `3d3c42e5aac5ba805825da76410c181273ba90b1` |
| gitleaks (CI) | 8.30.1 | GitHub gitleaks/gitleaks release | 2026-11-07 | Linux x64 tar SHA256 `551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb` from upstream checksums |

The `current` directory can move or lose an old ISO. The builder verifies the
recorded digest and refuses a different file rather than silently accepting the
new release. Refresh this pin with Debian's signed checksum file when upgrading.
The testing weekly netinst from 2026-11-02 is not used: its kernel/module mismatch
was observed in the original VM trial. Testing is reached by a verified in-target
upgrade from the working release installer.
