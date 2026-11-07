#!/bin/sh
# Repack the checksum-pinned signed Debian release installer. Never write disks.
# Production: local credentials + typed erase confirmation, explicit serial/DMI.
# vm-test: private throwaway credentials, only QEMU + XC0_TEST_DISK accepted.
set -eu
usage() {
    cat <<'EOF'
Usage: iso/build.sh NETINST.iso testing|stable [options]
  --hostname NAME           installed hostname (default loki for testing)
  --target-serial SERIAL     exact physical install disk serial (release mode)
  --target-dmi PRODUCT       exact DMI product_name (release mode)
  --stage2-ref COMMIT        reviewed post-install commit (default clean checkout HEAD)
  --authorized-keys FILE     optional explicit SSH public keys, not a GitHub account feed
  --vm-test                  unattended disposable VM test; never for physical disks
  --secrets-dir DIR          vm-test only: luks-passphrase, user-password-hash
  --output-dir DIR           output artifacts (default build/)
Release ISOs have no passwords. Network and credential prompts are local.
VM-test ISOs contain throwaway secrets: never publish or use them for loki.
EOF
}
if [ "${1:-}" = --help ]; then usage; exit 0; fi
[ "$#" -ge 2 ] || { usage >&2; exit 2; }
src=$1 profile=$2; shift 2
here=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
root=$(dirname "$here")
mode=release hostname='' serial='' dmi='' ref='' keys='' secrets='' out_dir=$root/build
while [ "$#" -gt 0 ]; do
    case "$1" in
        --hostname|--target-serial|--target-dmi|--stage2-ref|--authorized-keys|--secrets-dir|--output-dir)
            [ "$#" -ge 2 ] || { echo "missing value: $1" >&2; exit 2; }
            case "$1" in
                --hostname) hostname=$2 ;; --target-serial) serial=$2 ;;
                --target-dmi) dmi=$2 ;; --stage2-ref) ref=$2 ;;
                --authorized-keys) keys=$2 ;; --secrets-dir) secrets=$2 ;;
                --output-dir) out_dir=$2 ;;
            esac
            shift 2 ;;
        --vm-test) mode=vm-test; shift ;;
        *) echo "unknown option: $1" >&2; exit 2 ;;
    esac
done
case "$profile" in testing|stable) : ;; *) echo 'profile must be testing or stable' >&2; exit 2 ;; esac
for tool in xorriso cpio gzip python3 git md5sum sha256sum; do
    command -v "$tool" >/dev/null || { echo "missing tool: $tool" >&2; exit 2; }
done
if [ -z "$hostname" ]; then
    if [ "$mode" = vm-test ]; then hostname=xc0-$profile-vm
    elif [ "$profile" = testing ]; then hostname=loki
    else hostname=debian-hyprland; fi
fi
case "$hostname" in ''|*[!a-z0-9-]*|-*|*-) echo 'invalid hostname' >&2; exit 2 ;; esac
[ "${#hostname}" -le 63 ] || exit 2
if [ "$mode" = vm-test ]; then
    serial=XC0_TEST_DISK dmi=QEMU
    [ -n "$secrets" ] || { echo '--vm-test needs --secrets-dir' >&2; exit 2; }
    for f in luks-passphrase user-password-hash; do
        [ -s "$secrets/$f" ] || { echo "missing VM credential file: $f" >&2; exit 2; }
    done
else
    [ -z "$secrets" ] || { echo 'release mode refuses embedded credentials' >&2; exit 2; }
    [ -n "$serial" ] && [ -n "$dmi" ] || { echo 'release mode needs target serial AND DMI product' >&2; exit 2; }
fi
# Restrict config values rather than source arbitrary caller-supplied shell text.
case "$serial" in *[!a-zA-Z0-9_.:-]*) echo 'unsupported serial characters' >&2; exit 2 ;; esac
printf '%s\n' "$dmi" | LC_ALL=C grep -Eq '^[a-zA-Z0-9 ._:()+/-]+$' || {
    echo 'unsupported DMI characters' >&2; exit 2;
}
if [ -z "$ref" ]; then
    [ -z "$(git -C "$root" status --porcelain --untracked-files=normal)" ] || {
        echo 'commit the reviewed source before building, or provide --stage2-ref explicitly' >&2; exit 2;
    }
    ref=$(git -C "$root" rev-parse HEAD)
fi
[ "${#ref}" -eq 40 ] || { echo 'stage2 ref must be a full 40-character commit SHA' >&2; exit 2; }
case "$ref" in *[!0-9a-f]*) echo 'invalid commit SHA' >&2; exit 2 ;; esac
python3 "$here/verify-source.py" "$src" >&2
[ "$(git -C "$root" cat-file -t "$ref")" = commit ] || {
    echo 'post-install ref is not an available reviewed Git commit' >&2; exit 2;
}
umask 077
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT HUP INT TERM
mkdir -p "$work/root/xc0" "$work/root/usr/lib/partman/init.d" "$out_dir"
# Debian's initrd is merged-/usr. Never replace its /lib symlink with a directory.
# Shared directory modes must not inherit the private builder umask either.
chmod 0755 "$work/root" "$work/root/usr" "$work/root/usr/lib" \
    "$work/root/usr/lib/partman" "$work/root/usr/lib/partman/init.d"
out_dir=$(CDPATH='' cd -- "$out_dir" && pwd)
iso_id=xc0-$profile-$mode-$(printf '%.12s' "$ref")
cp "$here/preseed/base.cfg" "$work/root/preseed.cfg"
cp "$here/preseed/early.sh" "$here/preseed/select-disk.sh" \
    "$here/preseed/late.sh" "$here/preseed/confirm.templates" "$work/root/xc0/"
cp "$here/preseed/00xc0-guard" "$work/root/usr/lib/partman/init.d/00xc0-guard"
chmod 0755 "$work/root/usr/lib/partman/init.d/00xc0-guard"
printf "MODE='%s'\nPROFILE='%s'\nHOSTNAME='%s'\nTARGET_SERIAL='%s'\nTARGET_DMI='%s'\nSTAGE2_REF='%s'\nISO_ID='%s'\n" \
    "$mode" "$profile" "$hostname" "$serial" "$dmi" "$ref" "$iso_id" > "$work/root/xc0/install.conf"
: > "$work/root/xc0/authorized_keys"
if [ -n "$keys" ]; then
    python3 "$here/validate-public-keys.py" "$keys"
    ssh-keygen -lf "$keys" >/dev/null
    cp "$keys" "$work/root/xc0/authorized_keys"
fi
if [ "$mode" = vm-test ]; then
    # Reject multi-line/CR values, which would inject arbitrary preseed answers.
    python3 - "$secrets" "$work/root/preseed.cfg" <<'PY'
import pathlib, sys
s=pathlib.Path(sys.argv[1])
password=(s/'luks-passphrase').read_text().rstrip('\n')
hashed=(s/'user-password-hash').read_text().rstrip('\n')
if any(c in password+hashed for c in '\r\n') or len(password)<12 or not hashed.startswith(('$6$', '$y$')):
    sys.exit('VM credentials need a >=12 character passphrase and a crypt hash; no extra lines')
with open(sys.argv[2], 'a') as f:
    f.write('\nd-i partman-crypto/passphrase password '+password+'\n')
    f.write('d-i partman-crypto/passphrase-again password '+password+'\n')
    f.write('d-i passwd/user-password-crypted password '+hashed+'\n')
PY
fi
python3 - "$mode" "$profile" "$ref" "$iso_id" "$work/artifact.json" <<'PY'
import json, sys
mode, profile, ref, iso_id, path=sys.argv[1:]
with open(path,'w') as f:
    json.dump({'schema':1,'iso_id':iso_id,'mode':mode,'profile':profile,'config_ref':ref},f,sort_keys=True)
    f.write('\n')
PY
xorriso -osirrox on -indev "$src" \
    -extract /install.amd/gtk/initrd.gz "$work/orig.gz" \
    -extract /boot/grub/grub.cfg "$work/grub.orig" \
    -extract /md5sum.txt "$work/md5.orig" >"$work/extract.log" 2>&1
# Explicit intermediate files avoid masking a failed cpio command in a pipeline.
(cd "$work/root" && find . -print > "$work/filelist")
(cd "$work/root" && cpio -o -H newc -R 0:0 --quiet < "$work/filelist" > "$work/extra.cpio")
gzip -n -9 < "$work/extra.cpio" > "$work/extra.gz"
cat "$work/orig.gz" "$work/extra.gz" > "$work/initrd.gz"
timeout=-1
[ "$mode" != vm-test ] || timeout=3
awk -v id="$iso_id" -v seconds="$timeout" '
    !done && /^menuentry/ {
        print "set default=0"; print "set timeout=" seconds
        print "menuentry \"xc0 " id " - explicit target erase\" {"
        print "    linux /install.amd/vmlinuz auto=true priority=critical vga=788 --- quiet"
        print "    initrd /xc0/initrd.gz"
        print "}"; done=1
    }
    { print }
' "$work/grub.orig" > "$work/grub.cfg"
# Keep stock initrds/entries untouched: their interactive/recovery path must not preseed.
grep -vE ' ([.]?/)?boot/grub/grub.cfg$' "$work/md5.orig" > "$work/md5sum.txt"
{
    printf '%s  ./boot/grub/grub.cfg\n' "$(md5sum "$work/grub.cfg" | cut -d' ' -f1)"
    printf '%s  ./xc0/initrd.gz\n' "$(md5sum "$work/initrd.gz" | cut -d' ' -f1)"
    printf '%s  ./xc0/artifact.json\n' "$(md5sum "$work/artifact.json" | cut -d' ' -f1)"
} >> "$work/md5sum.txt"
out=$out_dir/$iso_id.iso
[ ! -e "$out" ] || { echo "refusing to overwrite existing artifact: $out" >&2; exit 2; }
partial=$out.partial
[ ! -e "$partial" ] || { echo "refusing to overwrite partial artifact: $partial" >&2; exit 2; }
if ! xorriso -indev "$src" -outdev "$partial" \
    -map "$work/initrd.gz" /xc0/initrd.gz \
    -map "$work/artifact.json" /xc0/artifact.json \
    -map "$work/grub.cfg" /boot/grub/grub.cfg \
    -map "$work/md5sum.txt" /md5sum.txt \
    -boot_image any replay >"$work/repack.log" 2>&1; then
    cat "$work/repack.log" >&2; exit 1
fi
chmod 0600 "$partial"
digest=$(sha256sum "$partial" | cut -d' ' -f1)
printf '%s  %s\n' "$digest" "$out" > "$out.sha256"
python3 - "$work/artifact.json" "$digest" "$out.manifest.json" <<'PY'
import json,sys
data=json.load(open(sys.argv[1]))
data['image_sha256']=sys.argv[2]
with open(sys.argv[3],'w') as f:
    json.dump(data,f,sort_keys=True)
    f.write('\n')
PY
# Publish the ISO only when its matching metadata is complete.
mv "$partial" "$out"
printf '%s\n' "$out"
