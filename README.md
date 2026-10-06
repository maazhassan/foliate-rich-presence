# foliate-rich-presence

Shows the book you're reading in [Foliate](https://github.com/johnfactotum/foliate) as a Discord Rich Presence (title, chapter, progress). Runs locally over Discord's IPC socket; no bot or server.

<img width="262" height="106" alt="Screenshot_20261006_010950" src="https://github.com/user-attachments/assets/94ab32bd-bdb7-4843-b2c4-d1211e2eebc9" />

Requires Python 3 with PyGObject and the AT-SPI typelib.

```
./foliate_rpc.py            # Foliate icon as image (upload foliate.png as an art asset named "foliate")
./foliate_rpc.py --covers   # upload book covers to a temporary public host (litterbox, 72h)
```

To run on login, create a systemd user service pointing at `foliate_rpc.py` and `systemctl --user enable --now` it.
