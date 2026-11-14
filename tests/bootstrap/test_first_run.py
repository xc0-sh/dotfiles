"""Isolated first-run checks: never sudo, authenticate, or bootstrap the host."""

import hashlib
import io
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "FIRST-RUN.sh"
TEMP_ROOT = Path("/tmp/opencode")
if not TEMP_ROOT.is_dir():
    TEMP_ROOT = Path(tempfile.gettempdir())

# Debian trixie snapper_0.10.6-1.2.debian.tar.xz: debian/80snapper.
SNAPPER_COMMON = (
    "if [ -e /etc/default/snapper ]; then . /etc/default/snapper; fi; "
    "if [ -x /usr/bin/snapper ] && [ ! x$DISABLE_APT_SNAPSHOT = 'xyes' ]"
)
SNAPPER_PRE = (
    SNAPPER_COMMON + " && [ -e /etc/snapper/configs/root ]; then "
    "rm -f /var/tmp/snapper-apt || true ; "
    "snapper create -d apt -c number -t pre -p > /var/tmp/snapper-apt || true ; "
    "snapper cleanup number || true ; fi"
)
SNAPPER_POST = (
    SNAPPER_COMMON + " && [ -e /var/tmp/snapper-apt ]; then "
    "snapper create -d apt -c number -t post --pre-number=`cat /var/tmp/snapper-apt` || true ; "
    "snapper cleanup number || true ; fi"
)
SNAPPER_HOOK = (
    "# https://bugs.debian.org/cgi-bin/bugreport.cgi?bug=770938\n"
    f'  DPkg::Pre-Invoke  {{ "{SNAPPER_PRE}"; }};\n'
    f'  DPkg::Post-Invoke {{ "{SNAPPER_POST}"; }};\n'
)
SNAPPER_EFFECTIVE = (
    f'DPkg::Pre-Invoke:: "{SNAPPER_PRE}";\n'
    f'DPkg::Post-Invoke:: "{SNAPPER_POST}";\n'
)


class FirstRunHarness:
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="first-run-test-", dir=TEMP_ROOT)
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name)

    def bash(self, body, *args, ok=True, env=None):
        result = subprocess.run(
            ["bash", "-c", 'source "$1"; shift; ' + body, "test", str(SCRIPT), *map(str, args)],
            text=True, capture_output=True, cwd=ROOT,
            env={**os.environ, **(env or {}), "PYTHONDONTWRITEBYTECODE": "1"},
        )
        if ok:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def run_bash(self, *args, ok=True):
        result = subprocess.run(
            ["bash", str(SCRIPT), *args],
            text=True,
            capture_output=True,
            cwd=ROOT,
        )
        if ok:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        return result


class FirstRunTests(FirstRunHarness, unittest.TestCase):
    def test_help_is_side_effect_free_and_documents_gates(self):
        result = self.run_bash("--help")
        self.assertIn("--check", result.stdout)
        self.assertIn("--apply", result.stdout)
        self.assertIn("NVIDIA packages", result.stdout)
        self.assertIn("Snapper", result.stdout)

    def test_invalid_cli_is_rejected_before_host_preflight(self):
        self.run_bash("--unknown", ok=False)
        self.run_bash("--check", "--apply", ok=False)
        self.run_bash("--check", "--xccode-release", "latest", ok=False)
        self.run_bash("--check", "--no-xccode", "--xccode-release", "v1.2.3", ok=False)

    def test_stage2_commit_parser_requires_exactly_one_literal_full_sha(self):
        ref = "57059da0f1b39c0ffdd0f0202bb63674be18061a"
        with tempfile.TemporaryDirectory(prefix="first-run-test-", dir=TEMP_ROOT) as temp:
            source = Path(temp) / "next-steps"
            source.write_text(
                "git clone https://github.com/xc0-sh/dotfiles.git ~/dotfiles\n"
                f"  git -C ~/dotfiles checkout --detach {ref}\n"
            )
            result = subprocess.run(
                ["bash", "-c", 'source "$1"; read_stage2_ref "$2"', "test", str(SCRIPT), str(source)],
                text=True,
                capture_output=True,
                cwd=ROOT,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), ref)

            source.write_text(
                f"git -C ~/dotfiles checkout --detach {ref}\n"
                f"git -C ~/dotfiles checkout --detach {ref}\n"
            )
            result = subprocess.run(
                ["bash", "-c", 'source "$1"; read_stage2_ref "$2"', "test", str(SCRIPT), str(source)],
                text=True,
                capture_output=True,
                cwd=ROOT,
            )
            self.assertNotEqual(result.returncode, 0)

    def test_signed_xccode_flow_uses_explicit_tag_and_two_integrity_checks(self):
        script = SCRIPT.read_text()
        self.assertIn('gh release download "$XCCODE_RELEASE" --repo xc0-sh/xccode', script)
        self.assertIn('gpgv --status-fd 1 --keyring', script)
        self.assertIn('sha256sum -c SHA256SUMS', script)
        self.assertIn('actual_sha == "$tei_sha"', script)
        self.assertNotIn('gh release download latest', script)

    def test_git_status_failure_with_empty_output_blocks(self):
        checkout = self.work / "dotfiles"
        checkout.mkdir()
        for status, output, blocked in ((0, "", 0), (0, " M FIRST-RUN.sh", 1), (128, "", 1)):
            with self.subTest(status=status, output=output):
                result = self.bash(
                    '''
                    HOME=$1
                    status_exit=$2 status_output=$3
                    ref=57059da0f1b39c0ffdd0f0202bb63674be18061a
                    id() { echo marius; }
                    hostname() { echo thor; }
                    grep() { return 0; }
                    read_stage2_ref() { echo "$ref"; }
                    git() {
                        case "$3 ${4:-}" in
                            "rev-parse --show-toplevel") echo "$HOME/dotfiles" ;;
                            "rev-parse HEAD") echo "$ref" ;;
                            "remote get-url") echo https://github.com/xc0-sh/dotfiles.git ;;
                            "status --porcelain=v1") printf "%s" "$status_output"; return "$status_exit" ;;
                            *) return 99 ;;
                        esac
                    }
                    check_base_and_checkout
                    printf "blocked=%s" "$BLOCKED"
                    ''', self.work, status, output,
                )
                self.assertIn(f"blocked={blocked}", result.stdout)
                if status:
                    self.assertIn("cannot inspect", result.stderr)

    def snapper_fixture(self):
        config = self.work / "root-config"
        hook = self.work / "80snapper"
        defaults = self.work / "defaults"
        helper = self.work / "snapper"
        effective = self.work / "apt-dump"
        config.write_text('SUBVOLUME="/"\nFSTYPE="btrfs"\n')
        hook.write_text(SNAPPER_HOOK)
        defaults.write_text('SNAPPER_CONFIGS="root"\nDISABLE_APT_SNAPSHOT="no"\n')
        helper.write_text("#!/bin/sh\nexit 99\n")  # Must never actually be executed.
        helper.chmod(0o755)
        effective.write_text(SNAPPER_EFFECTIVE)
        return config, hook, defaults, helper, effective

    def check_snapper_fixture(self, paths, *, apt_exit=0, snapper_exit=0, disabled=""):
        return self.bash(
            '''
            fixture_effective=$5 apt_exit=$6 snapper_exit=$7 disabled=$8
            DISABLE_APT_SNAPSHOT=$disabled
            is_installed() { return 0; }
            sudo() {
                case "$1" in
                    test|cat) "$@" ;;
                    apt-config) cat "$fixture_effective"; return "$apt_exit" ;;
                    sh) printf "%s" "$disabled" ;;
                    snapper) return "$snapper_exit" ;;
                    *) return 99 ;;
                esac
            }
            check_snapper "$1" "$2" "$3" "$4"
            printf "blocked=%s" "$BLOCKED"
            ''', *paths, apt_exit, snapper_exit, disabled,
        )

    def test_snapper_requires_stock_nonempty_and_effective_pre_post_hooks(self):
        paths = self.snapper_fixture()
        self.assertIn("blocked=0", self.check_snapper_fixture(paths).stdout)
        for text in ("", "# disabled\n", "// " + SNAPPER_HOOK.replace("\n", "\n// "),
                     SNAPPER_HOOK.replace(SNAPPER_PRE, "true"),
                     SNAPPER_HOOK.replace(SNAPPER_POST, "true")):
            with self.subTest(hook=text):
                paths[1].write_text(text)
                self.assertIn("blocked=1", self.check_snapper_fixture(paths).stdout)
        paths[1].write_text(SNAPPER_HOOK)
        for effective in ("", f'DPkg::Pre-Invoke:: "{SNAPPER_PRE}";\n',
                          f'DPkg::Post-Invoke:: "{SNAPPER_POST}";\n',
                          SNAPPER_EFFECTIVE.replace("DPkg::", "Comment::DPkg::"),
                          SNAPPER_EFFECTIVE.replace(SNAPPER_PRE, "true; " + SNAPPER_PRE)):
            paths[4].write_text(effective)
            self.assertIn("blocked=1", self.check_snapper_fixture(paths).stdout)
        paths[4].write_text(SNAPPER_EFFECTIVE)
        self.assertIn("blocked=1", self.check_snapper_fixture(paths, apt_exit=100).stdout)
        self.assertIn("blocked=1", self.check_snapper_fixture(paths, snapper_exit=1).stdout)
        paths[3].chmod(0o644)
        self.assertIn("blocked=1", self.check_snapper_fixture(paths).stdout)

    def test_snapper_defaults_are_not_sourced_and_cannot_inhibit_snapshots(self):
        paths = self.snapper_fixture()
        marker = self.work / "executed"
        for settings in (
            'DISABLE_APT_SNAPSHOT=yes\n',
            'export DISABLE_APT_SNAPSHOT="yes" # disabled\n',
            "DISABLE_APT_SNAPSHOT='yes'\nDISABLE_APT_SNAPSHOT=no\n",
            f'DISABLE_APT_SNAPSHOT=$(touch {marker})\n',
            f'touch {marker}\n',
            'PATH="/unreviewed"\n',
        ):
            with self.subTest(settings=settings):
                paths[2].write_text(settings)
                self.assertIn("blocked=1", self.check_snapper_fixture(paths).stdout)
                self.assertFalse(marker.exists())
        paths[2].write_text('# DISABLE_APT_SNAPSHOT=yes\nDISABLE_APT_SNAPSHOT="no"\n')
        self.assertIn("blocked=0", self.check_snapper_fixture(paths).stdout)
        self.assertIn("blocked=1", self.check_snapper_fixture(paths, disabled="yes").stdout)
        paths[2].unlink()
        self.assertIn("blocked=0", self.check_snapper_fixture(paths).stdout)

    def test_snapper_root_config_requires_literal_unique_root_subvolume(self):
        paths = self.snapper_fixture()
        marker = self.work / "executed"
        for config in ('# SUBVOLUME="/"\n', 'SUBVOLUME="/home"\n',
                       'SUBVOLUME="/"\nSUBVOLUME="/"\n',
                       f'SUBVOLUME=$(touch {marker})\n',
                       'SUBVOLUME="/"; false\n'):
            with self.subTest(config=config):
                paths[0].write_text(config)
                self.assertIn("blocked=1", self.check_snapper_fixture(paths).stdout)
                self.assertFalse(marker.exists())

    def test_malformed_or_foreign_gpg_status_is_rejected_even_with_exit_zero(self):
        fingerprint = "A" * 40
        valid = f"[GNUPG:] VALIDSIG {fingerprint} 2026-11-14 1794614400 0 4 0 22 8 00 {fingerprint}\n"
        status_file = self.work / "status"
        for status in ("", "[GNUPG:] GOODSIG A signer\n", "[GNUPG:] VALIDSIG\n",
                       valid.replace(" 4 0 ", " four 0 "), valid + "unexpected stdout\n",
                       valid + "[GNUPG:] BADSIG A signer\n", valid.rstrip() + " trailing\n",
                       valid.replace(f"00 {fingerprint}", "00 " + "B" * 40)):
            with self.subTest(status=status):
                status_file.write_text(status)
                self.bash(
                    'status_file=$1; XCCODE_FINGERPRINT=$2; '
                    'gpgv() { cat "$status_file"; }; '
                    'verify_xccode_signature /ignored ignored ignored',
                    status_file, fingerprint, ok=False,
                )
        status_file.write_text(valid)
        self.bash(
            'status_file=$1; XCCODE_FINGERPRINT=$2; '
            'gpgv() { cat "$status_file"; return 1; }; '
            'verify_xccode_signature /ignored ignored ignored', status_file, fingerprint, ok=False,
        )

    def test_no_xccode_path_does_not_download_or_install_xccode(self):
        result = self.bash(
            '''
            id() { echo marius; }
            check_base_and_checkout() { :; }
            check_snapper() { :; }
            check_physical_thor() { :; }
            check_nvidia_prepared() { :; }
            stage_xccode_release() { die "must not stage"; }
            check_xccode_tools() { die "must not authenticate"; }
            run_bootstrap() { echo "mock desktop $MODE"; }
            main --apply --no-xccode
            '''
        )
        self.assertIn("mock desktop apply", result.stdout)

    def test_nvidia_driver_install_and_mok_enrollment_are_not_automated(self):
        script = SCRIPT.read_text()
        self.assertIn('no driver installation or MOK enrollment', script)
        self.assertNotIn('mokutil --import', script)
        self.assertNotIn('apt-get install nvidia-open', script)


class ArchiveSafetyTests(FirstRunHarness, unittest.TestCase):
    def archive(self, entries):
        archive = self.work / "payload.tar.gz"
        with tarfile.open(archive, "w:gz") as tar:
            for fields in entries:
                entry = tarfile.TarInfo(fields.get("name", "install/example"))
                entry.uid, entry.gid = fields.get("uid", 0), fields.get("gid", 0)
                entry.uname, entry.gname = fields.get("uname", "root"), fields.get("gname", "root")
                entry.mode = fields.get("mode", 0o664)
                entry.type = fields.get("type", tarfile.REGTYPE)
                entry.linkname = fields.get("linkname", "")
                tar.addfile(entry)
        return archive

    def validate_archive(self, archive, *, ok=True):
        for privilege in ("", "root"):
            self.bash(
                'sudo() { [[ $1 == tar ]] || return 99; "$@"; }; '
                'validate_xccode_archive "$1" "$2"',
                archive, privilege, ok=ok,
            )

    def test_normal_root_git_archive_modes_are_accepted(self):
        archive = self.archive([
            {"name": "install/", "type": tarfile.DIRTYPE, "mode": 0o775},
            {"name": "install/example", "mode": 0o664},
            {"name": "install/installer", "mode": 0o775},
        ])
        self.validate_archive(archive)

    def test_unsafe_ownership_modes_paths_and_member_types_are_rejected(self):
        for fields in (
            {"uid": 1001}, {"gid": 1001}, {"uname": "marius"}, {"gname": "marius"},
            {"mode": 0o666}, {"mode": 0o4755}, {"mode": 0o2755},
            {"name": "/outside"}, {"name": "install/../../outside"},
            {"name": "install/./ambiguous"}, {"name": "install/line\nbreak"},
            {"type": tarfile.SYMTYPE, "linkname": "/etc/passwd"},
            {"type": tarfile.LNKTYPE, "linkname": "install/example"},
            {"type": tarfile.CHRTYPE}, {"type": tarfile.BLKTYPE},
            {"type": tarfile.FIFOTYPE},
        ):
            with self.subTest(fields=fields):
                self.validate_archive(self.archive([fields]), ok=False)
        self.validate_archive(self.archive([{}, {}]), ok=False)

    def test_manifest_requires_one_exact_basename_record_in_both_stages(self):
        manifest = self.work / "SHA256SUMS"
        digest = "a" * 64
        name = "xccode-v1.2.3.tar.gz"
        for text, valid in (
            (f"{digest}  {name}\n", True),
            (f"{digest} *{name}\n", True),
            (f"{digest}  another.tar.gz\n", False),
            (f"{digest}  ./{name}\n", False),
            (f"{digest}  /{name}\n", False),
            (f"{digest}  {name}\n" * 2, False),
            (f"not-a-digest  {name}\n", False),
        ):
            manifest.write_text(text)
            for privilege in ("", "root"):
                with self.subTest(text=text, privilege=privilege):
                    self.bash(
                        'sudo() { [[ $1 == cat ]] || return 99; "$@"; }; '
                        'validate_xccode_manifest "$1" "$2" "$3"',
                        manifest, name, privilege, ok=valid,
                    )


@unittest.skipUnless(shutil.which("gpg") and shutil.which("gpgv"), "GnuPG required for real signature fixtures")
class SignedReleaseTests(FirstRunHarness, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.key_temp = tempfile.TemporaryDirectory(prefix="first-run-keys-", dir=TEMP_ROOT)
        cls.addClassCleanup(cls.key_temp.cleanup)
        cls.key_home = Path(cls.key_temp.name)
        cls.key_home.chmod(0o700)
        cls.gpg_command = [
            "gpg", "--no-options", "--homedir", str(cls.key_home), "--batch", "--no-tty",
            "--pinentry-mode", "loopback", "--passphrase", "",
        ]
        cls.addClassCleanup(
            subprocess.run, ["gpgconf", "--homedir", str(cls.key_home), "--kill", "gpg-agent"],
            capture_output=True,
        )
        cls.gpg("--quick-generate-key", "First Run A <a@example.invalid>", "ed25519", "cert", "0")
        cls.primary = cls.fingerprint("a@example.invalid")
        cls.gpg("--quick-add-key", cls.primary, "ed25519", "sign", "0")
        cls.gpg("--quick-generate-key", "First Run B <b@example.invalid>", "ed25519", "sign", "0")
        cls.other = cls.fingerprint("b@example.invalid")
        cls.keyring = cls.key_home / "combined.gpg"
        cls.keyring.write_bytes(cls.gpg("--export", cls.primary, cls.other).stdout)

    @classmethod
    def gpg(cls, *args):
        return subprocess.run(cls.gpg_command + list(map(str, args)), capture_output=True, check=True)

    @classmethod
    def fingerprint(cls, identity):
        listing = cls.gpg("--with-colons", "--list-keys", identity).stdout.decode()
        return next(line.split(":")[9] for line in listing.splitlines() if line.startswith("fpr:"))

    def sign(self, payload, signer=None, suffix=".sig"):
        signature = Path(str(payload) + suffix)
        self.gpg("--local-user", signer or self.primary, "--output", signature, "--detach-sign", payload)
        return signature

    def verify(self, payload, signature, *, root=False, ok=True):
        return self.bash(
            '''
            XCCODE_KEYRING=$1 XCCODE_FINGERPRINT=$2
            validate_xccode_key
            (( BLOCKED == 0 ))
            sudo() { [[ $1 == gpgv ]] || return 99; "$@"; }
            verify_xccode_signature "$1" "$3" "$4" "$5"
            ''', self.keyring, self.primary, signature, payload, "root" if root else "",
            env={"GNUPGHOME": str(self.key_home)}, ok=ok,
        )

    def test_real_signing_subkey_is_accepted_in_user_and_root_verification(self):
        payload = self.work / "SHA256SUMS"
        payload.write_text("test signed payload\n")
        signature = self.sign(payload)
        for root in (False, True):
            self.verify(payload, signature, root=root)

    def test_relative_keyring_is_resolved_before_gpgv(self):
        self.bash(
            'XCCODE_KEYRING=$1 XCCODE_FINGERPRINT=$2; validate_xccode_key; '
            '(( BLOCKED == 0 )); [[ $XCCODE_KEYRING == "$3" ]]',
            os.path.relpath(self.keyring, ROOT), self.primary, self.keyring,
            env={"GNUPGHOME": str(self.key_home)},
        )

    def test_combined_keyring_does_not_authorize_second_primary(self):
        payload = self.work / "SHA256SUMS"
        payload.write_text("test signed payload\n")
        signature = self.sign(payload, self.other)
        for root in (False, True):
            result = self.verify(payload, signature, root=root, ok=False)
            self.assertIn("confirmed primary key", result.stderr)

    def test_real_signed_payload_tampering_is_rejected(self):
        payload = self.work / "SHA256SUMS"
        payload.write_text("original\n")
        signature = self.sign(payload)
        payload.write_text("tampered\n")
        for root in (False, True):
            self.verify(payload, signature, root=root, ok=False)

    def release_fixture(self, *, unsafe=None, manifest_name=None):
        assets = self.work / "assets"
        assets.mkdir()
        tei = assets / "text-embeddings-router"
        tei.write_bytes(b"test-only TEI binary\n")
        tei_sha = hashlib.sha256(tei.read_bytes()).hexdigest()
        archive = assets / "xccode-v1.2.3.tar.gz"
        files = {
            "etc/versions.lock": f'[tei]\nversion = "v1.2.3"\nsha256 = "{tei_sha}"\n',
            "install/install.sh": (
                '#!/bin/sh\nset -eu\n'
                'test -r "$(dirname "$0")/xcc"\n'
                'test -r "$(dirname "$0")/../etc/versions.lock"\n'
                'printf "%s|%s\\n" "$(umask)" "$*" >> "$TEST_INSTALL_LOG"\n'
            ),
            "install/fetch-release.sh": "#!/bin/sh\nexit 99\n",
            "install/xcc": "test-only private source\n",
        }
        with tarfile.open(archive, "w:gz") as tar:
            for name, data in files.items():
                entry = tarfile.TarInfo(name)
                entry.uid, entry.gid = 0, 0
                entry.mode = 0o775 if name.endswith(".sh") else 0o664
                entry.size = len(data.encode())
                tar.addfile(entry, io.BytesIO(data.encode()))
            if unsafe:
                entry = tarfile.TarInfo(unsafe.get("name", "install/unsafe"))
                entry.uid, entry.gid = unsafe.get("uid", 0), unsafe.get("gid", 0)
                entry.mode = unsafe.get("mode", 0o644)
                entry.type = unsafe.get("type", tarfile.REGTYPE)
                entry.linkname = unsafe.get("linkname", "")
                tar.addfile(entry)
        sums = assets / "SHA256SUMS"
        sums.write_text(f"{hashlib.sha256(archive.read_bytes()).hexdigest()}  {manifest_name or archive.name}\n")
        self.sign(sums)
        self.sign(sums, self.other, ".other.sig")
        return assets

    def stage_release(self, assets, *, replace_root_signature=False, ok=True):
        return self.bash(
            '''
            work=$1 assets=$2
            export TMPDIR=$work TEST_INSTALL_LOG=$work/installer.log
            XCCODE_KEYRING=$3 XCCODE_FINGERPRINT=$4 XCCODE_RELEASE=v1.2.3
            replace_signature=$5
            fixture_gid=$6
            id() { [[ $* == "-g marius" ]] || return 99; echo "$fixture_gid"; }
            check_xccode_tools() { :; }  # No gh auth/network access.
            gh() {
                [[ $1 == release && $2 == download && $6 == --dir ]] || return 99
                if [[ $3 == v1.2.3 ]]; then
                    cp "$assets/xccode-v1.2.3.tar.gz" "$assets/SHA256SUMS" "$assets/SHA256SUMS.sig" "$7/"
                else
                    [[ $3 == tei-v1.2.3 ]] || return 99
                    cp "$assets/text-embeddings-router" "$7/"
                fi
            }
            ask_to_continue() { [[ $1 == "INSTALL XCCODE v1.2.3" ]]; }
            sudo() {
                printf "%q " "$@" >> "$work/sudo.log"; printf "\\n" >> "$work/sudo.log"
                case "$1" in
                    mktemp) mktemp -d "$work/root.XXXXXXXX" ;;
                    chown) : ;;  # Assert requested root ownership; never require privilege.
                    chmod|install|gpgv|sha256sum|tar|find|cat)
                        # Every real write must be inside this disposable fixture.
                        [[ ${*: -1} == "$work/"* || $1 == find ]] || return 98
                        "$@"
                        if [[ $replace_signature == yes && $1 == install && ${*: -1} == */SHA256SUMS.sig ]]; then
                            cp "$assets/SHA256SUMS.other.sig" "${*: -1}"
                        fi
                        ;;
                    sh) [[ $2 == -c && $4 == _ && $5 == "$work/"* ]] || return 98; "$@" ;;
                    env) [[ $2 == "XCCODE_SIGNING_KEY=$work/"* ]] || return 98; "$@" ;;
                    *) return 99 ;;
                esac
            }
            stage_xccode_release
            printf "ROOT_STAGE=%s\\nSTAGE=%s\\nUMASK=%s\\n" "$ROOT_STAGE" "$STAGE" "$(umask)"
            ''', self.work, assets, self.keyring, self.primary,
            "yes" if replace_root_signature else "no", os.getgid(), ok=ok,
            env={"GNUPGHOME": str(self.key_home)},
        )

    def test_staging_normalizes_modes_and_scopes_installer_umask(self):
        result = self.stage_release(self.release_fixture())
        root = Path(next(line.split("=", 1)[1] for line in result.stdout.splitlines() if line.startswith("ROOT_STAGE=")))
        self.assertEqual(root.stat().st_mode & 0o7777, 0o710)
        self.assertEqual((root / "src").stat().st_mode & 0o7777, 0o750)
        for entry in (root / "src").rglob("*"):
            mode = entry.stat().st_mode & 0o7777
            expected = 0o750 if entry.is_dir() or entry.name.endswith(".sh") else 0o640
            self.assertEqual(mode, expected, entry)
            self.assertFalse(mode & 0o027, entry)  # No group writes or public access.
        for name in ("v1.2.3", "tei-v1.2.3"):
            self.assertEqual((root / name).stat().st_mode & 0o7777, 0o700)
            for entry in (root / name).iterdir():
                self.assertEqual(entry.stat().st_mode & 0o7777, 0o600)
        self.assertEqual((root / "signing-key").stat().st_mode & 0o7777, 0o600)
        log = (self.work / "sudo.log").read_text()
        self.assertIn(f"chown -R 0:{os.getgid()} ", log)
        self.assertIn("--no-same-owner --no-same-permissions", log)
        self.assertEqual((self.work / "installer.log").read_text().splitlines(), [
            "0022|--operator marius --release v1.2.3 --check",
            "0022|--operator marius --release v1.2.3",
        ])
        self.assertIn("UMASK=0077", result.stdout)

    def test_root_stage_rechecks_actual_signer_after_copy(self):
        result = self.stage_release(self.release_fixture(), replace_root_signature=True, ok=False)
        self.assertIn("confirmed primary key", result.stderr)
        self.assertFalse((self.work / "installer.log").exists())

    def test_archive_links_are_rejected_before_privileged_extraction(self):
        result = self.stage_release(self.release_fixture(unsafe={
            "type": tarfile.SYMTYPE, "linkname": "/etc/passwd",
        }), ok=False)
        self.assertIn("unsafe xccode archive", result.stderr)
        self.assertFalse((self.work / "installer.log").exists())

    def test_signed_manifest_must_cover_exact_selected_tarball(self):
        result = self.stage_release(self.release_fixture(manifest_name="another.tar.gz"), ok=False)
        self.assertIn("exact selected xccode tarball basename once", result.stderr)
        self.assertFalse((self.work / "installer.log").exists())

    def test_archive_tampering_is_rejected_by_the_signed_checksum(self):
        assets = self.release_fixture()
        archive = assets / "xccode-v1.2.3.tar.gz"
        archive.write_bytes(archive.read_bytes() + b"tampered\n")
        result = self.stage_release(assets, ok=False)
        self.assertIn("FAILED", result.stdout)
        self.assertFalse((self.work / "installer.log").exists())


if __name__ == "__main__":
    unittest.main()
