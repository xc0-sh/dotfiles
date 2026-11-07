"""Deterministic, unprivileged tests. Never run bootstrap against the host."""

import configparser
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
BOOTSTRAP = ROOT / "setup/bootstrap.sh"
LINKER = ROOT / "setup/link-config.sh"
TEMP_ROOT = Path("/tmp/opencode")
if not TEMP_ROOT.is_dir():
    TEMP_ROOT = Path(tempfile.gettempdir())


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="bootstrap-test-", dir=TEMP_ROOT)
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name)

    def bash(self, body, *args, source=BOOTSTRAP, ok=True):
        result = subprocess.run(
            ["bash", "-c", 'source "$1"; shift; ' + body, "test", str(source), *map(str, args)],
            text=True, capture_output=True, cwd=ROOT,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        )
        if ok:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def os_release(self, contents):
        path = self.work / "os-release"
        path.write_text(contents)
        return path

    def config_fixture(self):
        source = self.work / "staged"
        home = self.work / "home"
        (source / "desktop/app").mkdir(parents=True)
        home.mkdir()
        (source / "desktop/app/config").write_text("managed\n")
        (source / "manifest.txt").write_text("# comment\napp/config\n")
        return source, home

    def test_help_is_unprivileged_and_side_effect_free(self):
        for shell in ("bash", "sh"):
            result = subprocess.run([shell, str(BOOTSTRAP), "--help"], text=True, capture_output=True)
            self.assertEqual(result.returncode, 0)
            self.assertIn("--profile PROFILE --user USER", result.stdout)

    def test_bad_arguments(self):
        cases = [[], ["--profile", "generic"], ["--profile", "../loki", "--user", "alice"],
                 ["--profile", "generic", "--user", "root"], ["--profile", "generic", "--user", "-bad"],
                 ["--profile", "generic", "--user", "alice;id"], ["--profile"], ["--user"],
                 ["--profile", "qemu", "--profile", "loki", "--user", "alice"],
                 ["--profile", "qemu", "--user", "alice", "--nvidia-prepared"],
                 ["--profile", "qemu", "--user", "alice", "--unknown"]]
        for case in cases:
            with self.subTest(args=case):
                self.bash('parse_args "$@"', *case, ok=False)

    def test_profiles_map_to_fixed_codenames(self):
        for profile in ("loki", "thor", "baldr", "generic", "qemu"):
            result = self.bash('parse_args --profile "$1" --user alice; printf "%s" "$SUITE"', profile)
            self.assertEqual(result.stdout, "forky" if profile == "loki" else "trixie")

    def test_profile_aliases_derive_host_without_changing_it(self):
        for alias, host, profile, suite in (("testing", "loki", "loki", "forky"),
                                             ("testing", "vm-test", "testing", "forky"),
                                             ("stable", "baldr", "baldr", "trixie"),
                                             ("stable", "vm-test", "generic", "trixie"),
                                             ("stable", "thor", "thor", "trixie")):
            with self.subTest(alias=alias, host=host):
                result = self.bash('host=$2; hostname() { echo "$host"; }; '
                                   'if [[ $host == thor ]]; then extra=--nvidia-prepared; else extra=""; fi; '
                                   'parse_args --profile "$1" --user marius ${extra:+"$extra"}; '
                                   'printf "%s %s" "$PROFILE" "$SUITE"', alias, host)
                self.assertEqual(result.stdout, f"{profile} {suite}")
        self.bash('hostname() { echo loki; }; parse_args --profile stable --user marius', ok=False)
        self.bash('hostname() { echo thor; }; parse_args --profile testing --user marius', ok=False)

    def test_thor_requires_acknowledgement_and_prepared_debian_driver(self):
        self.bash('PROFILE=thor; check_nvidia_preparation', ok=False)
        self.bash('PROFILE=thor; NVIDIA_PREPARED=1; dpkg-query() { echo missing; }; check_nvidia_preparation', ok=False)
        self.bash('PROFILE=thor; NVIDIA_PREPARED=1; dpkg-query() { echo installed; }; check_nvidia_preparation')

    def test_debian_suite_guard(self):
        fixtures = [("debian", "trixie", "13", "generic", True),
                    ("debian", "forky", "", "loki", True),
                    ("arch", "", "", "loki", False), ("ubuntu", "trixie", "13", "generic", False),
                    ("debian", "forky", "", "generic", False),
                    ("debian", "trixie", "13", "loki", False),
                    ("debian", "trixie", "12", "generic", False)]
        for os_id, codename, version, profile, ok in fixtures:
            with self.subTest(os_id=os_id, codename=codename, profile=profile):
                path = self.os_release(f'ID="{os_id}"\nVERSION_CODENAME={codename}\nVERSION_ID="{version}"\n')
                self.bash('parse_args --profile "$1" --user alice; check_os_release "$2"', profile, path, ok=ok)

    def test_os_release_is_not_executed(self):
        marker = self.work / "executed"
        path = self.os_release(f'ID=$(touch {marker})\nVERSION_CODENAME=forky\n')
        self.bash('SUITE=forky; check_os_release "$1"', path, ok=False)
        self.assertFalse(marker.exists())

    def test_normal_user_validation_and_bad_homes(self):
        home = self.work / "alice"
        home.mkdir()
        # Stub the passwd record and stat only; no host account is changed.
        prefix = 'TARGET_USER=alice; getent() { printf "alice:x:%s:1000::%s:/bin/bash\\n" "$uid" "$home"; }; stat() { printf "%s\\n" "$owner"; }; '
        for uid, owner, valid in ((1000, 1000, True), (1001, 1001, True), (0, 0, False), (999, 999, False),
                                  (65534, 65534, False), (1000, 0, False)):
            with self.subTest(uid=uid, owner=owner):
                self.bash('uid=$1; owner=$2; home=$3; ' + prefix + 'check_user', uid, owner, home, ok=valid)
        self.bash('uid=1000; owner=1000; home=/; ' + prefix + 'check_user', ok=False)

    def test_stock_ifupdown_and_wifi_are_read_only(self):
        path = self.work / "interfaces"
        text = ('source /etc/network/interfaces.d/*\nauto lo\niface lo inet loopback\n'
                'allow-hotplug wlan0\niface wlan0 inet dhcp\n'
                '    wpa-ssid a network name\n    wpa-psk this-is-test-data\niface wlan0 inet6 auto\n')
        path.write_text(text)
        self.bash('check_ifupdown "$1"', path)
        self.assertEqual(path.read_text(), text)

    def test_custom_ifupdown_rejected_without_logging_secrets(self):
        path = self.work / "interfaces"
        for text in ('iface eth0 inet static\n address 192.0.2.1\n',
                     'iface eth0 inet dhcp\n post-up do-not-log-this\n',
                     'source /root/secret\n', 'iface wlan0 inet dhcp\n wpa-conf /private/path\n'):
            path.write_text(text)
            result = self.bash('check_ifupdown "$1"', path, ok=False)
            self.assertNotIn("do-not-log-this", result.stdout + result.stderr)
            self.assertNotIn("/private/path", result.stdout + result.stderr)
            self.assertEqual(path.read_text(), text)

    def test_package_selection_is_scoped(self):
        for profile in ("loki", "generic", "thor"):
            args = ["--profile", profile, "--user", "alice"]
            if profile == "thor":
                args.append("--nvidia-prepared")
            result = self.bash('parse_args "$@"; plan_packages; printf "%s\\n" "${INSTALL_PACKAGES[@]}"', *args)
            packages = result.stdout.splitlines()
            self.assertEqual(len(packages), len(set(packages)))
            self.assertIn("pipewire-audio", packages)
            self.assertIn("xwayland", packages)
            self.assertEqual("power-profiles-daemon" in packages, profile == "loki")
            self.assertEqual("hyprland/trixie-backports" in packages, profile != "loki")
            selected = [p.split("/")[0] for p in packages if "/" in p]
            self.assertNotIn("pipewire-audio", selected)
            self.assertFalse(any(p.startswith("linux-image") or p.startswith("nvidia") for p in packages))

    def test_malformed_package_entries_rejected(self):
        path = self.work / "packages"
        for text in ("--allow-unauthenticated\n", "foot unexpected\n", "$(id)\n", "foot/trixie-backports\n", "# empty\n"):
            path.write_text(text)
            self.bash('load_packages "$1" COMMON_PACKAGES', path, ok=False)

    def test_apt_suite_guard_rejects_mixed_repositories(self):
        good = ('release a=now\nrelease o=Debian,a=stable,n=trixie,l=Debian\n'
                'release o=Debian,a=stable-security,n=trixie-security\n'
                'release o=Debian Backports,a=stable-backports,n=trixie-backports\n')
        self.bash('SUITE=trixie; check_apt_suites <<< "$1"', good)
        for bad in (good + 'release o=Debian,n=sid\n', good + 'release o=Unknown,n=trixie\n', 'release a=now\n'):
            self.bash('SUITE=trixie; check_apt_suites <<< "$1"', bad, ok=False)
        self.bash('SUITE=forky; check_apt_suites <<< "$1"', good, ok=False)

    def test_dry_run_fixture_cannot_execute_mutations(self):
        # Source functions and explicitly substitute ONLY host preflight/keyring.
        # This is not execution of bootstrap on Arch or any other real host.
        marker = self.work / "mutated"
        fixture = self.os_release('ID=debian\nVERSION_CODENAME=trixie\nVERSION_ID=13\n')
        result = self.bash('fixture=$1; marker=$2; '
                           'preflight() { check_os_release "$fixture"; }; '
                           'archive_keyring() { echo /fixture/debian-keyring.gpg; }; '
                           'apt-get() { touch "$marker"; }; '
                           'runuser() { touch "$marker"; }; systemctl() { touch "$marker"; }; '
                           'main --profile generic --user alice --dry-run', fixture, marker)
        self.assertIn("hyprland/trixie-backports", result.stdout)
        self.assertIn("dry-run complete; nothing changed", result.stdout)
        self.assertFalse(marker.exists())
        self.assertNotIn("-t trixie-backports", result.stdout)

    def test_failed_apt_update_does_not_continue(self):
        trace = self.work / "trace"
        result = self.bash('trace=$1; apt-get() { echo apt >> "$trace"; return 42; }; '
                           'block_service_starts() { echo policy >> "$trace"; }; '
                           'install_packages; echo unexpected >> "$trace"', trace, ok=False)
        self.assertEqual(result.returncode, 42)
        self.assertEqual(trace.read_text(), "apt\n")

    def test_failed_install_is_propagated_and_cleanup_restores_policy(self):
        trace = self.work / "trace"
        result = self.bash('trace=$1; SUITE=trixie; '
                           'apt-get() { echo apt >> "$trace"; [[ " $* " != *" install "* ]] || return 43; }; '
                           'apt-cache() { echo "release o=Debian,n=trixie"; }; '
                           'block_service_starts() { echo blocked >> "$trace"; }; '
                           'restore_service_policy() { echo restored >> "$trace"; }; '
                           'trap cleanup EXIT; install_packages; echo unexpected >> "$trace"', trace, ok=False)
        self.assertEqual(result.returncode, 43)
        self.assertEqual(trace.read_text(), "apt\nblocked\napt\nrestored\n")
        self.assertIn("--no-remove", result.stdout)

    def test_service_policy_symlink_preserved_exactly(self):
        original = self.work / "original"
        original.write_text("original-policy\n")
        path = self.work / "policy-rc.d"
        path.symlink_to(original)
        self.bash('POLICY_PATH=$1; block_service_starts "$2"; '
                  '[[ ! -L $POLICY_PATH ]]; restore_service_policy', path, self.work)
        self.assertTrue(path.is_symlink())
        self.assertEqual(path.readlink(), original)
        self.assertEqual(original.read_text(), "original-policy\n")
        self.assertEqual(list(self.work.glob("xc0-policy.*")), [])

    def test_service_policy_absence_restored(self):
        path = self.work / "policy-rc.d"
        self.bash('POLICY_PATH=$1; block_service_starts "$2"; restore_service_policy', path, self.work)
        self.assertFalse(path.exists())

    def test_policy_restore_failure_retains_backup(self):
        path = self.work / "policy-rc.d"
        path.write_text("original\n")
        self.bash('POLICY_PATH=$1; block_service_starts "$2"; mv() { return 5; }; '
                  'if restore_service_policy; then exit 99; fi; '
                  '[[ -f $POLICY_DIR/original ]]; grep -qx original "$POLICY_DIR/original"', path, self.work)

    def test_system_config_preserved_and_idempotent(self):
        source = self.work / "source"
        source.write_text("new config\n")
        dest = self.work / "etc/app.conf"
        dest.parent.mkdir()
        dest.write_text("old config\n")
        backup = self.work / "backup"
        self.bash('BACKUP_DIR=$3; system_file "$1" "$2"; system_file "$1" "$2"', source, dest, backup)
        self.assertEqual(dest.read_text(), "new config\n")
        self.assertEqual((Path(str(backup) + str(dest))).read_text(), "old config\n")
        dest.unlink()
        dest.symlink_to(source)
        self.bash('BACKUP_DIR=$3; system_file "$1" "$2"', source, dest, backup, ok=False)

    def test_user_links_preserve_conflicts_and_unrelated_data(self):
        source, home = self.config_fixture()
        config = home / ".config/app"
        config.mkdir(parents=True)
        (config / "config").write_text("user data\n")
        (config / "config.xc0-backup").write_text("earlier backup\n")
        (config / "unrelated").write_text("keep\n")
        self.bash('link_config "$1" "$2"', source, home, source=LINKER)
        link = config / "config"
        timestamp = link.lstat().st_mtime_ns
        self.bash('link_config "$1" "$2"', source, home, source=LINKER)
        self.assertEqual(link.lstat().st_mtime_ns, timestamp)
        self.assertEqual(link.read_text(), "managed\n")
        self.assertEqual((config / "config.xc0-backup.1").read_text(), "user data\n")
        self.assertEqual((config / "unrelated").read_text(), "keep\n")

    def test_user_directory_and_dangling_leaf_symlink_preserved(self):
        for directory in (True, False):
            with self.subTest(directory=directory):
                source, home = self.config_fixture()
                dest = home / ".config/app/config"
                dest.parent.mkdir(parents=True)
                if directory:
                    dest.mkdir()
                    (dest / "data").write_text("keep")
                else:
                    dest.symlink_to("missing-user-target")
                self.bash('link_config "$1" "$2"', source, home, source=LINKER)
                saved = dest.with_name("config.xc0-backup")
                self.assertTrue(dest.is_symlink())
                if directory:
                    self.assertEqual((saved / "data").read_text(), "keep")
                else:
                    self.assertEqual(str(saved.readlink()), "missing-user-target")
                # Isolate the second fixture without deleting unrelated test data.
                self.work = self.work / "next"
                self.work.mkdir()

    def test_user_symlink_parent_refused(self):
        source, home = self.config_fixture()
        outside = self.work / "outside"
        outside.mkdir()
        (home / ".config").symlink_to(outside)
        self.bash('link_config "$1" "$2"', source, home, source=LINKER, ok=False)
        self.assertEqual(list(outside.iterdir()), [])

    def test_system_symlink_parent_refused(self):
        source = self.work / "source"
        source.write_text("config\n")
        outside = self.work / "outside"
        outside.mkdir()
        parent = self.work / "etc"
        parent.symlink_to(outside)
        self.bash('system_file "$1" "$2"', source, parent / "config", ok=False)
        self.assertEqual(list(outside.iterdir()), [])

    def test_backports_preferences_are_narrow_and_include_exact_abi_helpers(self):
        text = (ROOT / "setup/system/backports.pref").read_text()
        records = [r for r in text.split("\n\n") if "Package:" in r]
        broad = [r for r in records if "Package: *\n" in r]
        self.assertEqual(len(broad), 1)
        self.assertIn("Pin-Priority: 100", broad[0])
        self.assertNotIn("Pin-Priority: 500", text)
        self.assertIn("Package: libxkbcommon0 libxkbcommon-x11-0 libxkbregistry0", text)

    def test_config_manifest_and_autostart_are_consistent(self):
        manifest = [line for line in (ROOT / "config/manifest.txt").read_text().splitlines()
                    if line and not line.startswith("#")]
        actual = {str(p.relative_to(ROOT / "config/desktop"))
                  for p in (ROOT / "config/desktop").rglob("*") if p.is_file()}
        self.assertEqual(len(manifest), len(set(manifest)))
        self.assertEqual(set(manifest), actual)
        for entry in (ROOT / "config/desktop/autostart").glob("*.desktop"):
            desktop = configparser.ConfigParser(interpolation=None)
            desktop.read(entry)
            self.assertEqual(desktop["Desktop Entry"]["OnlyShowIn"], "Hyprland;")
            self.assertTrue(desktop["Desktop Entry"]["Exec"])
        bar = json.loads((ROOT / "config/desktop/waybar/config.jsonc").read_text())
        self.assertIn("tray", bar["modules-right"])


if __name__ == "__main__":
    unittest.main()
