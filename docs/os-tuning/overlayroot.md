# Optional overlayroot (power-loss safe reads)

Safydo already uses `journal_data_ordered` on ext4 and tmpfs logs (Phase 0).
For extra protection against SD wear or corruption, Raspberry Pi OS supports
**overlayroot** (root FS in RAM, read-only SD underneath).

## Enable (advanced)

```bash
sudo apt install overlayroot
echo 'overlayroot="tmpfs"' | sudo tee /etc/overlayroot.conf
sudo reboot
```

## Trade-offs

- Survives sudden power loss well; SD writes minimized
- Custom `/opt/rpi-audio-player` changes need overlayroot-chroot or re-deploy after reboot
- `player.yaml` edits via Web UI may not persist unless state is on writable path

## Recommended for Safydo

Default **ext4 tuning only** is enough for most users. Use overlayroot only if
the Pi is powered from a battery with frequent hard shutdowns.