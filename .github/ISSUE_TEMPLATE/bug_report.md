---
name: Bug report
about: Something doesn't work
---

**Environment**
- Ubuntu version:
- Session type (`echo $XDG_SESSION_TYPE`):
- GPU / driver:
- Phone model + Android version:
- Connection: Wi-Fi (2.4/5 GHz?) / USB

**Diagnostics (paste all of these)**

```
# on the PC
ubudesk doctor --json
server/.venv/bin/python server/scripts/portal_probe.py
```

```
# server log of the failing attempt
ubudesk serve --log-level debug
```

```
# phone log during the failing attempt
adb logcat -s UbuDesk
```

**What happened**

**What you expected**
