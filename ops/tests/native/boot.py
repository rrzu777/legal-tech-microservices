"""Boot one disposable Ubuntu VM; no host kernel/cgroups/swap access required."""
import json
import os
from pathlib import Path
import subprocess


def main():
    work = Path('/work')
    # Refuse reusing an earlier test disk or key; the runner owns this container.
    for name in ('guest.qcow2', 'id_ed25519', 'user-data', 'meta-data', 'seed.img'):
        if (work / name).exists():
            raise SystemExit('Refusing to reuse VM state; create a new test container')
    subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '',
                    '-f', str(work / 'id_ed25519')], check=True)
    config = {
        'ssh_pwauth': False,
        'ssh_genkeytypes': ['ed25519'],
        'disable_root': True,
        'ssh_authorized_keys': [(work / 'id_ed25519.pub').read_text().strip()],
        'package_update': True,
        'packages': ['git', 'jq', 'curl', 'cron', 'xvfb', 'xauth', 'dbus-user-session'],
        'runcmd': [
            ['mkdir', '-p', '/mnt/payload'],
            ['mount', '-t', '9p', '-o', 'trans=virtio,version=9p2000.L,ro',
             'payload', '/mnt/payload'],
            ['sh', '-ec', 'for tool in git jq curl xvfb-run xauth busctl; do command -v "$tool"; done; mountpoint -q /mnt/payload; touch /run/native-ready'],
        ],
    }
    (work / 'user-data').write_text('#cloud-config\n' + json.dumps(config))
    (work / 'meta-data').write_text('instance-id: resource-guards-native\nlocal-hostname: native-guards\n')
    subprocess.run(['cloud-localds', str(work / 'seed.img'),
                    str(work / 'user-data'), str(work / 'meta-data')], check=True)
    subprocess.run(['qemu-img', 'create', '-f', 'qcow2', '-F', 'qcow2',
                    '-b', '/image/base.qcow2', str(work / 'guest.qcow2'), '24G'], check=True)
    arguments = [
        'qemu-system-aarch64', '-machine', 'virt', '-cpu', 'cortex-a72',
        '-accel', 'tcg,thread=multi', '-smp', '2', '-m', '8192',
        '-bios', '/usr/share/qemu-efi-aarch64/QEMU_EFI.fd',
        '-drive', 'if=virtio,format=qcow2,file=/work/guest.qcow2',
        '-drive', 'if=virtio,format=raw,file=/work/seed.img,readonly=on',
        '-virtfs', 'local,path=/payload,mount_tag=payload,security_model=none,readonly=on',
        '-netdev', 'user,id=net0,hostfwd=tcp:127.0.0.1:2222-:22',
        '-device', 'virtio-net-device,netdev=net0',
        '-nographic', '-monitor', 'none', '-no-reboot',
    ]
    os.execvp(arguments[0], arguments)


if __name__ == '__main__':
    main()
