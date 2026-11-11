# Changelog






## 0.12.0 (app) · FL script unchanged (0.6.4)

Install this one by hand on every laptop. After this, Studio Sync updates itself.

* Updates: Studio Sync checks for a new version when it opens and every 6 hours. Click Update and it downloads, checks and installs the new version, then reopens. Downloads come from the public milanszel/studio-sync-releases repo; the code stays private.
* The FL script updates with the app. When a new app version opens, it replaces an installed older FL script (keeping a backup) and asks you to restart FL Studio.

## 0.11.1 (app) · FL script unchanged (0.6.4)

* A laptop whose clock was set more than 30 seconds off couldn't stay connected; it kept rejoining until the host blocked it with "keeps trying to join". Every laptop now uses the host's clock for its packets, so clock settings no longer matter.
* A laptop that has already proved the room code is never blocked for rejoining. Only real wrong guesses count toward the limit.

## 0.11.0 (app) · 0.6.4 (FL script)

Update every laptop and reinstall the FL script on each.

* A laptop with an older FL script can no longer change the room's tempo. Older scripts corrected drift by changing FL's speed or tempo, which spread to everyone as a BPM jump. That laptop shows in red and is put back on the room tempo.
* Diagnostics shows the last tempo change: from what, to what, by which laptop and when. It is also written to the log.
* Mac installer builds again (GitHub retired the macOS 14 machines).

## 0.10.4 (app) · 0.6.3 (FL script)

Reinstall the FL script on every laptop.

* Earlier/Later and drift fixes changed the BPM for everyone (one laptop ended up at 407 BPM). FL's playback speed control misbehaves in some FL versions. Corrections no longer touch speed or tempo at all: each one is a tiny move of a few ms on the next bar line.

## 0.10.3 (app) · 0.6.1 (FL script)

Update every laptop and reinstall the FL script on each.

* Tempos above 300 BPM (or below 30) were rejected with "FL response rejected". Any tempo FL allows, 10 to 522 BPM, now syncs.

## 0.10.2 (app) · FL script unchanged

* Windows installer: no more "close Studio Sync" prompts. It ends any Studio Sync left running in the background before installing or uninstalling.
* Closing the window always quits the app, even if cleanup hangs, so no invisible copy stays running.

## 0.10.1 (app) · 0.6.0 (FL script)

* Partner View memory: the viewer swapped in a new image URL 4 times a second, which the Mac window could keep forever; it now downloads a frame only when it changed and frees the previous one. Mac capture now grabs the window at the size that is sent (not full Retina size) and converts each frame inside its own memory pool, so no native image memory is left behind.


## 0.10.0 (app) · 0.6.0 (FL script)

Update every laptop and reinstall the FL script on each.

* Exporting while connected no longer hangs or crashes FL. When FL stops answering (exporting, a dialog, loading) the app stops sending it anything but a slow ping, and the script never touches the transport until FL is responsive again.
* Much tighter sync. Playheads are compared after smoothing (FL reports positions in audio buffer steps), the tolerance went from 150 ms to 8 ms, and small errors are fixed by playing a fraction of a percent faster or slower for a few seconds instead of jumping. Jumps only happen past 80 ms, on a bar line.
* Earlier / Later buttons on the main screen move a laptop in 5 ms steps while playing, to line up laptops whose audio comes out at different speeds.
* The main screen shows In time, Catching up or Leading.
* Warns when laptops use very different audio buffer sizes.
* Timing settings can be changed while playing.


## 0.9.0 (app) · 0.5.1 (FL script)

Update every laptop. The FL script is unchanged from 0.8.1.

* Room codes are 6 characters (K7Q-M2P). Joining uses a SPAKE2 handshake, so a short code stays safe: guesses can only be made live against the host, which limits them and warns you.
* A wrong code says so right away instead of waiting.
* After joining, the room runs on a random secret from the host; a restarted host is rejoined automatically.
* GitHub Actions builds the Mac dmg and Windows Setup exe for every release.


## 0.8.1 (app) · 0.5.1 (FL script)

* Installers: `Build-Mac-App.command` makes and installs Studio Sync.app plus a dmg; `Build-Windows-Installer.bat` makes a Setup exe (Start menu, firewall rule, FL script, loopMIDI and WebView2 checks). Both run a self test on the built app first.
* Installed apps log to `~/.studio-sync/studio-sync.log`.

Reinstall the FL script on every laptop.

* Host tempo changes not reaching guests: the FL script now sets tempo two ways (mixer.setCurrentTempo, then a tempo control event that every FL version accepts), checks half a second later that it took, and if FL refused it, every laptop shows which one and its FL API number.
* FL finishing a tempo change from the app no longer counts as a local edit on that laptop.
* Diagnostics lists each laptop's FL API number.


## 0.8.0 (app) · 0.5.0 (FL script)

Update every laptop and reinstall the FL script on each.

**More laptops**
* Up to 8 laptops per room. The host relays who is in the room; files and Partner View go to everyone directly.
* If the laptop that started playback leaves, the host takes over without a jump.
* Late joiners come in at the right place.

**Reliability**
* A lost connection no longer stops anyone. Playback continues and resyncs on return; edits made while away are ignored.
* If the app closes, FL keeps playing where it is (it used to stop and jump to the start).
* Drift corrections happen on the next bar line.
* An unconfirmed local edit stops blocking after 2 s.
* A large audio offset no longer makes a late start come in behind; it starts at the right position.
* Clock estimate uses the fastest trip in each direction over 10 s and slews small changes.
* No drift corrections for 1 s after a tempo change.
* Start mode: Together (shared count in) or Instant (no stutter on the laptop that pressed Play).

**Security**
* Room codes are 12 random characters; keys are derived with scrypt.
* Packets carry a time and counter; replays are rejected. File requests use one time nonces.

**Other**
* Discovery searches every local network (WiFi and Ethernet).
* Clear messages for a full room, a blocked firewall, wrong laptop clocks and older versions.
* Partner View sends smaller frames, only when they change, and half as often while playing.
* Received filenames that Windows can't save (CON, AUX, ...) are refused; free disk space is checked first.
* Only one copy of the app can run.


## 0.7.0 (app) · 0.4.0 (FL script)

Update both laptops and reinstall the FL script on both.

**Sync**
* Network errors no longer end the session. Windows connection resets and WiFi drops are reported and the session continues.
* MIDI and network messages are timestamped on arrival, not when the loop reaches them.
* Shared starts are sent to FL at the due moment and run in OnMidiIn. OnIdle is only a fallback.
* The sync loop wakes as soon as a packet arrives instead of polling.
* FL stops playback after 5 s without the app (was 3 s).
* Diagnostics counts drift corrections (audible jumps).

**Security**
* The room code is never sent. Packets and file requests are signed with an HMAC of it.
* Find Host no longer reveals the room code. Type it from the host screen.

**Performance**
* Audio key listening is opt in and remembered.
* Key detection overtone analysis is vectorized.
* The app hands the Python GIL over more often so the sync thread is not held up.

**Removed**
* Screen note reader and captured ghost notes. Share MIDI files instead.
* Legacy single request file transfer (whole file in memory).
* Old Tkinter interface (`app.py`).

**Fixes**
* Mac Partner View uses ScreenCaptureKit instead of the obsolete CGWindowListCreateImage.
* Received files keep their names.
* Received audio supports byte ranges, so the preview player can seek.
* File dialogs and disk writes no longer freeze the window.
* Settings are saved atomically.
* Version checks use protocol numbers instead of hardcoded version strings.
* Launchers reinstall dependencies only when requirements change.
* Windows key detection explains that it hears the default output only, not ASIO.

## 0.6.x

* 0.6.24: Partner View drag to pan, 4 fps capture.
* 0.6.18 to 0.6.23: expanded and floating Partner View, zoom, local preview, view source choice.
* 0.6.16: pattern loop phase sync.
* 0.6.13 to 0.6.15: chunked file sharing up to 500 MB, Collaborate tabs.
* 0.6.0 to 0.6.7: window sharing, automatic key listening, Mac native memory cleanup.

## 0.4.x to 0.5.x

* Desktop window, connection diagnostics, host discovery, ECG style connection monitor.
* Audio key detection on Mac (ScreenCaptureKit) and Windows (WASAPI loopback), shared between laptops.
