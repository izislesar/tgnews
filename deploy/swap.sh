#!/bin/bash
# Своп для сервера без swap: без него OOM на 1.9 ГБ убивает sshd/сервисы.
set -euo pipefail
SIZE="${1:-2G}"
if swapon --show | grep -q '/swapfile'; then
  echo "swap уже включён"; swapon --show; exit 0
fi
fallocate -l "$SIZE" /swapfile || dd if=/dev/zero of=/swapfile bs=1M count=2048
chmod 600 /swapfile
mkswap /swapfile
swapon /swapfile
grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
sysctl -w vm.swappiness=20
grep -q '^vm.swappiness' /etc/sysctl.conf || echo 'vm.swappiness=20' >> /etc/sysctl.conf
echo "swap готов:"
swapon --show
