#!/system/bin/sh

PT=/tmp/production_tool
SSH_DIR=/data/local/ssh
mkdir -p "$SSH_DIR" /root/.ssh
chmod 700 "$SSH_DIR" /root/.ssh
exec >>"$SSH_DIR/setup.log" 2>&1

echo "$(date) SSH setup started"

if [ ! -x /usr/sbin/sshd ]; then
    echo "sshd is missing"
    exit 1
fi
if [ ! -s "$PT/ssh/authorized_keys" ]; then
    echo "authorized_keys is missing"
    exit 1
fi

# Cube J1's bundled sshd needs this ARM library. Keep the boot-time copy on USB.
if [ ! -f /lib/libcrypto.so.1.0.0 ]; then
    cp "$PT/ssh/libcrypto.so.1.0.0" /lib/libcrypto.so.1.0.0 || exit 1
    chmod 644 /lib/libcrypto.so.1.0.0
fi

cp "$PT/ssh/authorized_keys" /root/.ssh/authorized_keys || exit 1
chmod 600 /root/.ssh/authorized_keys
cp "$PT/ssh/sshd_config" "$SSH_DIR/sshd_config" || exit 1
chmod 600 "$SSH_DIR/sshd_config"

if [ ! -s "$SSH_DIR/ssh_host_ed25519_key" ]; then
    ssh-keygen -q -t ed25519 -N '' -f "$SSH_DIR/ssh_host_ed25519_key" || exit 1
fi
chmod 600 "$SSH_DIR/ssh_host_ed25519_key"

/usr/sbin/sshd -t -f "$SSH_DIR/sshd_config" || exit 1
if [ -s "$SSH_DIR/sshd.pid" ] && kill -0 "$(cat "$SSH_DIR/sshd.pid")" 2>/dev/null; then
    echo "sshd is already running"
    exit 0
fi
/usr/sbin/sshd -f "$SSH_DIR/sshd_config" -E "$SSH_DIR/sshd.log" || exit 1
echo "$(date) sshd started"
