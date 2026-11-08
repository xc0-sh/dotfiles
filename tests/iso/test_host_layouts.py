"""Host policy, pure layout and file-backed identity/copy tests. No block devices."""
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest

ROOT = Path(__file__).resolve().parents[2]
PRESEED = ROOT / "iso/preseed"
TEMP_ROOT = Path("/tmp/opencode")
if not TEMP_ROOT.is_dir():
    TEMP_ROOT = Path(tempfile.gettempdir())


class HostLayoutTests(unittest.TestCase):
    def test_approved_policy_and_exact_subvolume_sets(self):
        policies = json.loads((ROOT / "iso/hosts.json").read_text())
        expected = {
            "thor": ["@:/", "@home:/home", "@snapshots:/.snapshots", "@var_log:/var/log",
                     "@var_tmp:/var/tmp", "@aios:/var/lib/aios", "@srv_aios:/srv/aios",
                     "@xccode:/var/lib/xcloud/xccode"],
            "loki": ["@:/", "@home:/home", "@pkg:/var/cache/pacman/pkg", "@log:/var/log", "@swap:/swap"],
        }
        for host, pairs in expected.items():
            result = subprocess.run(["sh", str(PRESEED / "btrfs.sh"), "--print-layout", host],
                                    text=True, capture_output=True, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.splitlines(), pairs)
            policy = policies[host]
            self.assertEqual(policy["layout"], host)
            self.assertEqual(policy["profile"], "stable" if host == "thor" else "testing")
            self.assertEqual([policy[k] for k in ("marius_uid", "marius_gid", "xcloud_uid", "xcloud_gid")],
                             ["1001", "1001", "1000", "1000"])

    def test_uuid_fstab_has_correct_boots_options_and_no_swap_device(self):
        for host in ("loki", "thor"):
            result = subprocess.run(["sh", str(PRESEED / "btrfs.sh"), "--fstab", host,
                                     "root-uuid", "boot-uuid", "esp-uuid"],
                                    text=True, capture_output=True, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            entries = {fields[1]: fields for line in result.stdout.splitlines()
                       if not line.startswith("#") and (fields := line.split())}
            self.assertTrue(all(fields[0].startswith("UUID=") for fields in entries.values()))
            self.assertFalse(any(fields[2] == "swap" for fields in entries.values()))
            level = "1" if host == "thor" else "3"
            for mountpoint, fields in entries.items():
                if fields[2] == "btrfs" and mountpoint != "/swap":
                    self.assertIn("noatime,compress=zstd:" + level, fields[3])
            self.assertEqual(entries["/boot"][0:3], ["UUID=boot-uuid", "/boot", "ext4"])
            self.assertEqual(entries["/boot/efi"][0:3], ["UUID=esp-uuid", "/boot/efi", "vfat"])
            if host == "loki":
                self.assertEqual(entries["/swap"][3], "subvol=@swap,noatime")
                self.assertNotIn("nodatacow", entries["/swap"][3])
        bad = subprocess.run(["sh", str(PRESEED / "btrfs.sh"), "--fstab", "loki", "bad uuid", "boot", "esp"],
                             capture_output=True, text=True)
        self.assertNotEqual(bad.returncode, 0)
        self.assertEqual(bad.stdout, "")
        empty = subprocess.run(["sh", str(PRESEED / "btrfs.sh"), "--fstab", "loki", "", "boot", "esp"],
                               capture_output=True, text=True)
        self.assertNotEqual(empty.returncode, 0)
        self.assertEqual(empty.stdout, "")

    def test_recipes_no_swap_pv_trap_or_unapproved_data_disk(self):
        for host in ("thor", "loki"):
            recipe = (PRESEED / f"recipes/{host}.cfg").read_text()
            self.assertIn(f"partman-auto/choose_recipe select xc0-{host}-crypto-btrfs", recipe)
            self.assertIn(f"xc0-{host}-crypto-btrfs ::", recipe)
            self.assertIn("1075 1075 1075 free", recipe)
            self.assertIn("$lvmok{ } lv_name{ root }", recipe)
            self.assertEqual(recipe.count("filesystem{ btrfs }"), 1)
            for forbidden in ("method{ swap }", "method{ lvm }", "in_vg{", "WDS200", "@games"):
                self.assertNotIn(forbidden, recipe)
        self.assertIn("1075 1075 1075 ext4", (PRESEED / "recipes/thor.cfg").read_text())
        self.assertIn("1075 1075 1075 ext4", (PRESEED / "recipes/loki.cfg").read_text())

    def test_strict_reshape_and_pre_kernel_hook_are_not_desktop_setup(self):
        script = (PRESEED / "btrfs.sh").read_text()
        self.assertIn("/proc/self/mountinfo", script)
        self.assertIn('mount --bind "$path" "$work/park/$number"', script)
        self.assertIn('mount -o "remount,bind,$flags" "$path"', script)
        self.assertIn('sort -r "$work/unmount.raw"', script)
        self.assertIn('chroot "$top/@" chattr +C /mnt/xc0-subvol', script)
        self.assertIn('dmsetup rename "$old_crypt" xcloud_root_crypt', script)
        self.assertLess(script.index("unsupported nested subvolumes"), script.index('btrfs subvolume snapshot "$top"'))
        for forbidden in ("umount -l", "mkfs", "wipefs", "sfdisk", "snapper", "WDS200"):
            self.assertNotIn(forbidden, script)
        hook = (PRESEED / "90xc0-boot").read_text()
        self.assertIn("while :; do sleep 3600", hook)
        prepare = (PRESEED / "boot-prepare.sh").read_text()
        self.assertIn('case "$LAYOUT" in thor|loki) esp_mount=/target/boot/efi', prepare)
        self.assertIn('boot=$(awk', prepare)
        self.assertNotIn('dpkg-divert', prepare)
        base = (PRESEED / "base.cfg").read_text()
        self.assertLess(base.index("sh -eu /xc0/btrfs.sh"), base.index("in-target sh /root/xc0-late.sh"))


class MetadataCopyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="xc0-copy-metadata-", dir=TEMP_ROOT)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "source"
        self.destination = self.root / "destination"
        self.source.mkdir(mode=0o750)
        self.destination.mkdir()
        (self.source / ".hidden").write_bytes(b"preserve hidden base files")
        (self.source / ".hidden").chmod(0o640)
        os.link(self.source / ".hidden", self.source / "hardlink")
        (self.source / "symlink").symlink_to(".hidden")
        self.xattrs = True
        try:
            os.setxattr(self.source, "user.xc0", b"directory metadata")
            os.setxattr(self.source / ".hidden", "user.xc0", b"file metadata")
        except OSError:
            self.xattrs = False
        script = (PRESEED / "btrfs.sh").read_text()
        self.body = script.split('chroot "$top/@" /bin/sh -eu -c \'\n', 1)[1].split("\n    ' sh \"$mp\"", 1)[0]
        self.body = self.body.replace("/mnt/xc0-subvol", str(self.destination))

    def run_copy(self, **changes):
        return subprocess.run(["sh", "-eu", "-c", self.body, "sh", str(self.source)],
                              text=True, capture_output=True, env=dict(os.environ, **changes))

    def test_real_gnu_copy_preserves_metadata_links_hidden_files_before_removing_source(self):
        before = self.source.stat()
        result = self.run_copy()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(list(self.source.iterdir()), [])
        after = self.destination.stat()
        self.assertEqual((before.st_uid, before.st_gid, stat.S_IMODE(before.st_mode), int(before.st_mtime)),
                         (after.st_uid, after.st_gid, stat.S_IMODE(after.st_mode), int(after.st_mtime)))
        self.assertEqual((self.destination / ".hidden").read_bytes(), b"preserve hidden base files")
        self.assertEqual((self.destination / ".hidden").stat().st_ino, (self.destination / "hardlink").stat().st_ino)
        self.assertEqual(os.readlink(self.destination / "symlink"), ".hidden")
        if self.xattrs:
            self.assertEqual(os.getxattr(self.destination, "user.xc0"), b"directory metadata")
            self.assertEqual(os.getxattr(self.destination / ".hidden", "user.xc0"), b"file metadata")

    def test_copy_or_compare_failure_never_deletes_original_contents(self):
        binaries = self.root / "bin"
        binaries.mkdir()
        for tool in ("cp", "diff"):
            with self.subTest(tool=tool):
                stub = binaries / tool
                stub.write_text("#!/bin/sh\nexit 23\n")
                stub.chmod(0o755)
                result = self.run_copy(PATH=str(binaries) + os.pathsep + os.environ["PATH"])
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual((self.source / ".hidden").read_bytes(), b"preserve hidden base files")
                stub.unlink()

    def test_file_bind_backing_files_are_not_truncated_or_symlink_followed(self):
        target = self.root / "target"
        (target / "etc").mkdir(parents=True)
        work = self.root / "work"
        (work / "park").mkdir(parents=True)
        (work / "park/1").touch()
        script = (PRESEED / "btrfs.sh").read_text()
        body = script.split('    relative=${path#/target/}', 1)[1].split('    mount --bind "$work/park/$number" "$path"', 1)[0]
        body = ('relative=${path#/target/}' + body).replace("/target", str(target))
        destination = target / "etc/resolv.conf"
        destination.write_bytes(b"preserve the underlying resolver file")
        command = ['sh', '-eu', '-c', 'die() { exit 1; }; path=$1; work=$2; number=1; ' + body,
                   'sh', str(destination), str(work)]
        result = subprocess.run(command, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(destination.read_bytes(), b"preserve the underlying resolver file")
        destination.unlink()
        outside = self.root / "outside"
        outside.write_bytes(b"must not be followed")
        destination.symlink_to(outside)
        self.assertNotEqual(subprocess.run(command, capture_output=True).returncode, 0)
        self.assertEqual(outside.read_bytes(), b"must not be followed")


class BootPreparationFixtureTests(unittest.TestCase):
    """Run the early hook with fail-closed command mocks and ordinary files."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="xc0-boot-prepare-", dir=TEMP_ROOT)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.target = self.root / "target"
        for directory in ("boot", "boot/efi", "etc", "usr/sbin", "var/log"):
            (self.target / directory).mkdir(parents=True)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.esp = self.root / "esp-files"
        self.esp.mkdir()
        for name in ("disk", "esp-device", "boot-device"):
            (self.root / name).touch()
        (self.root / "authorized").write_text(str(self.root / "disk") + "|fixture\n")
        self.config = self.root / "install.conf"
        self.config.write_text("LAYOUT=loki\nHOSTNAME=loki\n")
        (self.root / "select-disk.sh").write_text("#!/bin/sh\n[ \"$1\" = --validate-authorized ]\n")
        (self.target / "etc/fstab").write_text("UUID=root / btrfs defaults 0 0\nUUID=boot /boot ext4 defaults 0 2\nUUID=esp /boot/efi vfat defaults 0 2\n")
        self.mounts = self.root / "mounts"
        self.mounts.write_text(f"{self.root}/boot-device {self.target}/boot ext4 rw 0 0\n"
                               f"{self.root}/esp-device {self.target}/boot/efi vfat rw 0 0\n")
        replacements = {
            "/xc0/install.conf": str(self.config), "/xc0/select-disk.sh": str(self.root / "select-disk.sh"),
            "/tmp/xc0-authorized": str(self.root / "authorized"),
            "/proc/mounts": str(self.mounts), "/target": str(self.target),
        }
        script = (PRESEED / "boot-prepare.sh").read_text()
        self.assertEqual(script.count("[ -b "), 2)
        script = script.replace("[ -b ", "[ -f ")  # private fixture copy only
        script = re.sub("|".join(re.escape(key) for key in sorted(replacements, key=len, reverse=True)),
                        lambda match: replacements[match[0]], script)
        self.script = self.root / "boot-prepare.sh"
        self.script.write_text(script)
        commands = textwrap.dedent(r'''
            import json, os
            from pathlib import Path
            import sys
            root = Path(os.environ['XC0_BOOT_ROOT'])
            name, args = Path(sys.argv[0]).name, sys.argv[1:]
            with (root / 'commands.jsonl').open('a') as log:
                log.write(json.dumps([name, *args]) + '\n')
            def refuse(): sys.exit('unmocked boot operation: ' + repr([name, *args]))
            if name == 'in-target':
                if args and args[0] == '--pass-stdout': args.pop(0)
                tool = args.pop(0)
                if tool == 'lsblk' and args[:3] == ['-ndo', 'PKNAME', '--']:
                    if args[-1] not in [str(root / 'esp-device'), str(root / 'boot-device')]: refuse()
                    print(os.environ.get('XC0_PARENT', 'xc0-fixture-disk'))
                elif tool == 'blockdev' and args[0] == '--getsize64':
                    if Path(args[-1]).name == os.environ.get('XC0_BAD_PARTITION'):
                        print('1072693248')
                    else:
                        print('1073741824')
                elif tool == 'blkid' and args == ['-p', '-s', 'PTTYPE', '-o', 'value', str(root / 'disk')]:
                    print(os.environ.get('XC0_TABLE', 'gpt'))
                elif tool == 'dpkg-divert' and args == ['--local', '--add', '--rename', '--divert',
                                                       '/usr/sbin/grub-install.xc0-real', '/usr/sbin/grub-install']:
                    pass
                else: refuse()
            elif name == 'readlink':
                if args == ['-f', '/dev/xc0-fixture-disk']: print(root / 'disk')
                elif args == ['-f', str(root / 'disk')]: print(root / 'disk')
                elif args == ['-f', '/dev/not-authorized']: print(root / 'other-disk')
                else: refuse()
            elif name == 'umount':
                if args != [str(root / 'target/boot/efi')]: refuse()
            elif name == 'mount':
                if args != ['-t', 'vfat', '-o', 'umask=0077', str(root / 'esp-device'), str(root / 'target/boot')]: refuse()
                path = root / 'target/boot'
                path.rmdir()
                path.symlink_to(root / 'esp-files', target_is_directory=True)
            else: refuse()
        ''')
        for name in ("in-target", "readlink"):
            executable = self.bin / name
            executable.write_text(f"#!{sys.executable}\n" + commands)
            executable.chmod(0o755)

    def run_hook(self, **changes):
        env = dict(os.environ, PATH=str(self.bin) + os.pathsep + os.environ["PATH"],
                   XC0_BOOT_ROOT=str(self.root), **changes)
        return subprocess.run(["sh", str(self.script)], env=env, capture_output=True, text=True, timeout=10)

    def events(self):
        path = self.root / "commands.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def test_gpt_parent_and_boot_partition_size_rejections_fail_closed(self):
        for changes in ({"XC0_TABLE": "dos"}, {"XC0_PARENT": "not-authorized"},
                        {"XC0_BAD_PARTITION": "boot-device"}):
            with self.subTest(changes=changes):
                result = self.run_hook(**changes)
                self.assertNotEqual(result.returncode, 0)
                log = (self.target / "var/log/xc0-boot-prepare.log").read_text()
                self.assertNotIn("/boot partition validated", log)
                self.assertEqual((self.target / "etc/fstab").read_text().count("UUID="), 3)
                self.assertIn("xc0 boot preparation exit: status=1", log)
                (self.root / "commands.jsonl").unlink(missing_ok=True)
                (self.target / "var/log/xc0-boot-prepare.log").unlink(missing_ok=True)

    def test_both_hosts_validate_the_same_esp_and_ext4_boot_architecture(self):
        for host in ("loki", "thor"):
            with self.subTest(host=host):
                self.config.write_text(f"LAYOUT={host}\nHOSTNAME={host}\n")
                result = self.run_hook()
                self.assertEqual(result.returncode, 0, result.stderr)
                log = (self.target / "var/log/xc0-boot-prepare.log").read_text()
                self.assertIn("exact 1 GiB ESP validated", log)
                self.assertIn(f"{host} /boot partition validated", log)
                events = self.events()
                self.assertEqual(sum(1 for event in events if event[0] == "in-target"
                                     and "blockdev" in event), 2)
                self.assertFalse(any(event[0] in ("mount", "umount") for event in events))
                self.assertIn("/boot ext4", (self.target / "etc/fstab").read_text())
                self.assertIn("/boot/efi vfat", (self.target / "etc/fstab").read_text())
                (self.root / "commands.jsonl").unlink(missing_ok=True)
                (self.target / "var/log/xc0-boot-prepare.log").unlink(missing_ok=True)


class AccountFixtureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="xc0-identities-", dir=TEMP_ROOT)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for name in ("home/marius", "etc/sudoers.d", "bin"):
            (self.root / name).mkdir(parents=True)
        (self.root / "etc/sudoers").write_text("%sudo ALL=(ALL:ALL) ALL\n")
        self.state = self.root / "state.json"
        self.write_state({"users": {"marius": [1001, 1001]}, "groups": {"marius": 1001, "sudo": 27},
                          "sudo": ["marius"], "locked": []})
        config = self.root / "install.conf"
        config.write_text("MARIUS_UID=1001\nMARIUS_GID=1001\nXCLOUD_UID=1000\nXCLOUD_GID=1000\n")
        script = (PRESEED / "accounts.sh").read_text()
        replacements = {"/root/xc0-install.conf": str(config), "/home": str(self.root / "home"),
                        "/etc/sudoers.d": str(self.root / "etc/sudoers.d"),
                        "/etc/sudoers": str(self.root / "etc/sudoers")}
        pattern = "|".join(re.escape(path) for path in sorted(replacements, key=len, reverse=True))
        self.script = self.root / "accounts.sh"
        self.script.write_text(re.sub(pattern, lambda match: replacements[match[0]], script))
        self.log = self.root / "commands.jsonl"
        commands = textwrap.dedent(r'''
            import json, os
            from pathlib import Path
            import sys
            root = Path(os.environ['XC0_ACCOUNT_ROOT'])
            path = root / 'state.json'
            state = json.loads(path.read_text())
            command, args = Path(sys.argv[0]).name, sys.argv[1:]
            with (root / 'commands.jsonl').open('a') as stream:
                stream.write(json.dumps([command, *args]) + '\n')
            def save(): path.write_text(json.dumps(state))
            def refuse(): sys.exit('unmocked account operation: ' + command + ' ' + repr(args))
            if command == 'getent':
                database, query = args
                if database not in ('passwd', 'group'): refuse()
                entries = state['users' if database == 'passwd' else 'groups']
                found = next(((name, value) for name, value in entries.items()
                              if name == query or str(value[0] if database == 'passwd' else value) == query), None)
                if found is None: sys.exit(2)
                name, value = found
                if database == 'passwd':
                    print(f'{name}:x:{value[0]}:{value[1]}::{root}/home/{name}:/bin/bash')
                else: print(f'{name}:x:{value}:')
            elif command == 'groupadd':
                if len(args) != 3 or args[0] != '--gid': refuse()
                gid, name = int(args[1]), args[2]
                if gid in state['groups'].values() or name in state['groups']: sys.exit(9)
                state['groups'][name] = gid
                save()
            elif command == 'useradd':
                if args != ['--uid', '1000', '--gid', '1000', '--create-home', '--shell', '/bin/bash', 'xcloud']: refuse()
                if state['groups'].get('xcloud') != 1000 or state['groups'].get('marius') != 1001: refuse()
                state['users']['xcloud'] = [1000, 1000]
                (root / 'home/xcloud').mkdir()
                save()
            elif command == 'passwd':
                if args == ['--lock', 'xcloud']:
                    state['locked'] = ['xcloud']; save()
                elif len(args) == 2 and args[0] == '-S':
                    name = args[1]
                    print(name + (' L ' if name in state['locked'] else ' P ') + '2026-11-07 0 99999 7 -1')
                else: refuse()
            elif command == 'usermod':
                if args[:2] != ['-aG', 'sudo'] or args[2] not in ('marius', 'xcloud'): refuse()
                if args[2] not in state['sudo']: state['sudo'].append(args[2])
                save()
            elif command == 'id':
                option, name = args
                if option == '-u': print(state['users'][name][0])
                elif option == '-g': print(state['users'][name][1])
                elif option == '-nG': print(name + (' sudo' if name in state['sudo'] else ''))
                else: refuse()
            elif command == 'stat':
                name = Path(args[-1]).name
                if not str(args[-1]).startswith(str(root / 'home')): refuse()
                print(':'.join(map(str, state['users'].get(name, [1000, 1000]))))
            elif command == 'find':
                if not args or not args[0].startswith(str(root / 'home')): refuse()
                if os.environ.get('XC0_BAD_HOME_CONTENTS'): print(str(root / 'home/marius/bad-owner'))
            elif command == 'visudo':
                if args != ['-c'] and (len(args) != 2 or args[0] != '-cf' or not args[1].startswith(str(root))): refuse()
            else: refuse()
        ''')
        for command in ("getent", "groupadd", "useradd", "passwd", "usermod", "id", "stat", "find", "visudo"):
            executable = self.root / "bin" / command
            executable.write_text(f"#!{sys.executable}\n" + commands)
            executable.chmod(0o755)

    def write_state(self, value):
        self.state.write_text(json.dumps(value))

    def run_accounts(self, *args, **changes):
        env = dict(os.environ, PATH=str(self.root / "bin") + os.pathsep + os.environ["PATH"],
                   XC0_ACCOUNT_ROOT=str(self.root), **changes)
        return subprocess.run(["sh", str(self.script), *args], text=True, capture_output=True, env=env, timeout=10)

    def mutations(self):
        return [entry for line in self.log.read_text().splitlines()
                if (entry := json.loads(line))[0] in ("groupadd", "useradd", "usermod", "passwd")
                and not (entry[0] == "passwd" and entry[1] == "-S")]

    def test_named_groups_precede_user_creation_and_locked_admin_has_exact_ids(self):
        state = json.loads(self.state.read_text())
        del state["groups"]["marius"]
        self.write_state(state)
        result = self.run_accounts()
        self.assertEqual(result.returncode, 0, result.stderr)
        events = self.mutations()
        self.assertEqual(events[:3], [["groupadd", "--gid", "1001", "marius"],
                                     ["groupadd", "--gid", "1000", "xcloud"],
                                     ["useradd", "--uid", "1000", "--gid", "1000", "--create-home", "--shell", "/bin/bash", "xcloud"]])
        self.assertEqual(json.loads(self.state.read_text())["locked"], ["xcloud"])
        rule = self.root / "etc/sudoers.d/50-xc0-users"
        self.assertEqual(rule.read_text(), "marius,xcloud ALL=(ALL:ALL) PASSWD: ALL\n")
        self.assertEqual(stat.S_IMODE(rule.stat().st_mode), 0o440)
        self.log.unlink()
        self.assertEqual(self.run_accounts("--check").returncode, 0)
        self.assertEqual(self.mutations(), [])

    def test_numeric_or_named_collisions_fail_before_mutations(self):
        original = json.loads(self.state.read_text())
        for database, name, value in (("users", "other", [1000, 1000]), ("groups", "other", 1000),
                                      ("users", "marius", [1000, 1001]), ("groups", "marius", 1000)):
            with self.subTest(database=database, name=name):
                state = json.loads(json.dumps(original))
                state[database][name] = value
                self.write_state(state)
                result = self.run_accounts()
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(self.mutations(), [])
                self.log.unlink()

    def test_bad_home_ownership_or_passwordless_policy_fails_check(self):
        self.assertEqual(self.run_accounts().returncode, 0)
        self.assertNotEqual(self.run_accounts("--check", XC0_BAD_HOME_CONTENTS="1").returncode, 0)
        (self.root / "etc/sudoers.d/old-vm").write_text("marius ALL=(ALL) NOPASSWD: ALL\n")
        self.assertNotEqual(self.run_accounts("--check").returncode, 0)


if __name__ == "__main__":
    unittest.main()
