# HomeKit Steam User Switcher

This script exposes a virtual HomeKit Television accessory where each input corresponds to a Steam user. Selecting an input updates the Steam’s AutoLoginUser; turning the "TV" off restarts Steam.

## Why

We regularly game on a headless machine and wanted to switch Steam users easily without fiddling with a KVM. HomeKit provides a convenient way to control devices, and by exposing Steam user accounts as HomeKit inputs, we can seamlessly switch users with our existing HomeKit setup and even set up personalized scenes.

## What it does
- HomeKit Television service (Active + ActiveIdentifier)
- Inputs are Steam users (PersonaName as label, AccountName as slug)
- When you switch input, AutoLoginUser is set accordingly
- When you turn the TV off, Steam is restarted (killall steam)
- Pairing/state stored under XDG state: `~/.local/state/homekit-steam-user-switcher/`

## Requirements
- Linux with Python 3.9+

## Install (user scope)

1) Install dependencies
```zsh
pip install --user -r requirements.txt
```

2) Install the script into your user bin directory
```zsh
install -Dm755 homekit_steam_user_switcher.py ~/.local/bin/homekit_steam_user_switcher.py
```

3) Create a systemd user unit
```zsh
install -Dm644 homekit-steam-user-switcher.service ~/.config/systemd/user/homekit-steam-user-switcher.service
systemctl --user daemon-reload
systemctl --user enable --now homekit-steam-user-switcher.service
```

Logs:
```zsh
journalctl --user -u homekit-steam-user-switcher.service -f
```

## Pairing
- Pairing code: `111-11-111`
- In Apple Home: Add Accessory -> More Options -> select the accessory named by `--name` (default "Steam Switcher").

## Advanced
- Change name/port/bind IP, or pass manual inputs:
```zsh
~/.local/bin/homekit_steam_user_switcher.py --name "Steam Switcher" --port 51826 --bind auto
~/.local/bin/homekit_steam_user_switcher.py --inputs "Alice,Bob"
```

## Reset pairing
Delete the state directory:
```zsh
rm -rf ~/.local/state/homekit-steam-user-switcher
```

## Uninstall
```zsh
systemctl --user disable --now homekit-steam-user-switcher.service
rm -f ~/.config/systemd/user/homekit-steam-user-switcher.service
rm -f ~/.local/bin/homekit_steam_user_switcher.py
rm -rf ~/.local/state/homekit-steam-user-switcher
```

## License
MIT
