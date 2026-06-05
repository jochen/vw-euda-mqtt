# systemd user service

## Install

```bash
mkdir -p ~/.config/systemd/user
cp systemd/vw-euda-mqtt.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now vw-euda-mqtt
```

Enable auto-start at boot (without being logged in):

```bash
loginctl enable-linger pi
```

## Logs

```bash
journalctl --user -u vw-euda-mqtt -f
```

## Stop / restart

```bash
systemctl --user stop vw-euda-mqtt
systemctl --user restart vw-euda-mqtt
systemctl --user status vw-euda-mqtt
```
