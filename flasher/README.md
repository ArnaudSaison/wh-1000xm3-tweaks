# XM3 Workbench

A local web UI for checking the headset, building quieter announcements, uploading them, and changing button timings.

```sh
uv run flasher/app.py          # opens http://127.0.0.1:8750
```

## What it does

- **Headset status:** battery, firmware, sound mode, equalizer, codec, custom button, touch panel, voice guidance, region. Only GET commands are used.
- **Quieter announcements:** downloads Sony's voice package and verifies its SHA-1, attenuates voice and beeps separately, and plays before/after samples.
- **Upload:** sends a voice package to the headset with live progress.
- **Double press for pairing:** a tick box in the same section adds the patch from `tools/double_press.py` to the build, with its own emulator rehearsal. A first version works on a headset; the current one, with a wait before the pairing sound, is emulated only.
- **Button timings:** sliders for the power button's short and long holds and two unidentified inputs, rule checks, a live state diagram, and an emulator rehearsal on each build. Flashes from your region's Sony file. Only a 4 s long hold has been tried on a real headset.
- **Simulate:** runs the whole upload sequence against a simulated headset, with no hardware.
- **Raw survey:** every read-only query with its raw reply.

Working files are kept in `work/ui/`, which git ignores.

## Sony's download key

Downloading Sony's original files means reading Sony's encrypted file list. The key is **not included** in this project. It has been published for years in [lzghzr/MDR_Proxy](https://github.com/lzghzr/MDR_Proxy), in the file `mdr.ts`. Getting it and using it is your own decision and your own risk.

Set it before starting the page (two keys: separate them with a comma):

```powershell
$env:XM3_INFOXML_KEYS = "<key in hex>"     # Windows PowerShell
```
```sh
export XM3_INFOXML_KEYS="<key in hex>"     # bash
```

Without it, downloading shows an error and everything else works.

## Trying the quiet announcements

A -9 dB package has been uploaded to a real headset and works. An upload can still fail, and recovery may require opening the headset. Afterwards, the headset once reported 0% battery until it was power-cycled or charged.

1. Charge above 50%, unplug the USB cable, turn the headset on, and connect.
2. Press **Test update channel**. It opens the update connection and closes it without sending data.
3. Upload **Sony's original** package first, since that proves the path with a file the headset must accept.
4. Build a quiet package, listen to it, and upload it.
5. To undo it, upload Sony's original again.

## Safety

- Before enabling an upload, it checks the model, battery above the headset's reported threshold, a valid package, and matching language. You must also type a confirmation word.
- The voice upload accepts only voice packages: exactly partition 3, sub-image 1, the right variant and a valid checksum. A full firmware file is refused there.
- The firmware upload accepts only files built by this page, kept in their own folder. It checks the model, firmware 4.5.2, battery, and that signatures, checksums and region pass offline, and needs its own confirmation word. A file that is accepted but doesn't boot can only be recovered through SPI, which isn't set up yet.
- A quiet package is verified to differ from its source only in audio files and the checksum word, and is re-parsed after building.
- The server binds to localhost, refuses requests addressed to any other host, and requires a per-run token on every POST.

## Files

| File | Role |
|---|---|
| `app.py` | Flask routes |
| `device.py` | Headset connection, status, upload jobs |
| `templates/index.html` | The page |
| `../xm3/package.py` | Package parsing, attenuation, download, firmware checks |
| `../tools/dfu_sign.py` | Builds the pairing hold patch |

The earlier version impersonated Sony's update server through an HTTP proxy. Current app versions force HTTPS, so that route no longer works and was removed; it remains in git history.
