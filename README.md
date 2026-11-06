# Studio Sync 0.10.4

Links up to 8 FL Studio sessions on the same local network so play, pause, position, tempo and loop mode stay together. It also sends files, shows your partner's FL window, and estimates the key of what is playing.

**Upgrading:** update the app on **every** laptop and reinstall the FL script on each (Settings > Install, then click *Update MIDI scripts* in FL). Every laptop needs the same version.

## Installing

Build the installers once, on one Mac and one Windows PC, then use them on every laptop.

**Mac:** double click `Build-Mac-App.command`. It builds Studio Sync, puts it in Applications, installs the FL script, and makes `dist/Studio-Sync-VERSION-Mac-arm64.dmg` (or `x86_64` on an Intel Mac) for the other Macs. A Mac with Apple silicon builds for Apple silicon Macs only; build on an Intel Mac for Intel Macs.

**Windows:** double click `Build-Windows-Installer.bat`. It builds Studio Sync and makes `dist\Studio-Sync-Setup-VERSION.exe` (it installs Inno Setup with winget if needed). The installer puts Studio Sync in Program Files with a Start menu entry, installs the FL script, opens the firewall for Studio Sync, and offers the loopMIDI and WebView2 downloads if they're missing.

Both builds need Python 3.12 and an internet connection the first time, and check the finished app before packaging it.

The installers are not signed with a paid certificate yet, so the first launch on another computer shows a warning:

* **Mac:** open the dmg, drag Studio Sync to Applications, open it once, then go to System Settings > Privacy & Security and click *Open Anyway*.
* **Windows:** on "Windows protected your PC", click *More info*, then *Run anyway*.

An installed app keeps its log in `~/.studio-sync/studio-sync.log`.

To run from source instead, use `Start-Mac.command` or `Start-Windows.bat`.

## Requirements

* Python 3.12 to build or run from source ([installer](https://www.python.org/downloads/release/python-31210/))
* Mac: macOS 14 Sonoma or later
* Windows: [loopMIDI](https://www.tobias-erichsen.de/software/loopmidi.html) with two ports named `Studio Sync To FL` and `Studio Sync From FL`
* All laptops on the same network, with date and time set automatically

## Setup (once per laptop)

1. Install Studio Sync (above). The installers also install the FL script when FL's user data folder is in the usual place.
2. If the FL script wasn't installed, in Settings click Install and choose your FL Studio **user data** folder (usually Documents/Image-Line/FL Studio).
3. In FL, open Options > MIDI settings:
   * Input: enable `Studio Sync To FL`, controller type **Studio Sync Transport**, port 42.
   * Output: enable `Studio Sync From FL`, port 42.
   * Leave external clock and master sync off.
4. The FL script output should say `Studio Sync 0.6.3 loaded`.
5. Use the **same audio buffer size** in FL (Options > Audio settings) on every laptop. FL reports its playhead in buffer sized steps, so different sizes leave laptops a constant few ms apart. Studio Sync tells you if they differ.

## Connecting

1. On one laptop choose **Host** and Connect. Its screen shows the room code, like `K7Q-M2P`.
2. On each other laptop choose **Join**, click Find Host, type that room code, and Connect. Up to 8 laptops fit in a room.
3. Play, pause, seek and change tempo from FL on any laptop. Everyone follows.

The room code is short but never sent over the network. Joining uses a SPAKE2 handshake (the method magic wormhole uses): each try at a code needs a live exchange with the host, nothing recorded off the WiFi helps anyone guess, and the host stops answering a laptop after 10 tries a minute (and warns you). After joining, every packet and file is signed with a random room secret, stamped with the time and a counter, so nobody else can join, replay old packets, or send files. Use *New code* for a fresh code; lower case and the dash are optional.

If a laptop loses WiFi, everyone keeps playing. It catches up when it returns, and anything changed on it while it was away is ignored. If the laptop that started playback leaves, the host takes over. Laptops that join mid song come in at the right place.

## Timing

* **Start wait** (Host, 100 to 1,000 ms): how far ahead a shared start is scheduled. Raise it if the opening gets cut off on a slow network.
* **Earlier / Later** (main screen, each laptop, 5 ms steps): if one laptop always sounds a little behind or ahead, tap these while playing until it lines up. It glides there, no jump.
* **Start mode** (Host): *Together* holds a pressed Play for a short shared count in so everyone starts at once. *Instant* starts the laptop you pressed Play on right away; the others come in a moment later at the right position.

Starts are timed by the app and sent to FL at the due moment. While playing, each laptop compares its own playhead with the room's (smoothed over the last second, since a single FL report can be a whole buffer off). Within 8 ms counts as in time. Further off, FL plays up to 0.8% faster or slower for a few seconds until it has caught up, which you don't hear. Only more than 80 ms off jumps back, on the next bar line. The main screen shows *In time*, *Catching up* or *Leading*; Diagnostics counts the nudges and jumps.

**Exporting:** while FL exports (or shows a dialog) it stops answering, and Studio Sync leaves it completely alone until it's back, then catches up.

Sync is not sample accurate. To measure it: record both laptops' outputs into one interface, one per channel, play a click, and compare the offset at the start and after 5 minutes.

## Collaborate

**Files:** send audio, MIDI or ZIP up to 500 MB to everyone in the room at once. Files arrive in 2 MB chunks, are checked with SHA-256, and keep their names (`Beat (2).wav` if the name is taken). To share notes, drag a pattern out of FL's piano roll as MIDI and send that file.

**Partner View:** click Share window to send your FL window to everyone, about 4 times a second (2 while playing, and only when it changes). When several people share, pick whose window to watch. Pop out keeps it floating above FL. Zoom and drag to pan.

## Audio key

Click Listen to estimate the key of FL's audio. It stays off until you turn it on and remembers your choice. Everyone else sees your estimate. Only the text summary is sent.

* **Mac:** captures FL's audio only. Allow Screen Recording for Studio Sync when asked, then reopen the app. After rebuilding the app, macOS may ask again.
* **Windows:** captures everything on the default Windows output device. FL must play through that device. ASIO output bypasses it and cannot be heard. Mute other apps while listening.

## Troubleshooting

* *No FL response:* check both MIDI ports, port 42, and the controller type.
* *Another laptop runs an older Studio Sync:* update every laptop.
* *No reply from the host:* check the address and code, and allow Python through the host's firewall (Windows asks the first time; Mac: System Settings > Network > Firewall).
* *Laptop clocks differ:* turn on automatic date and time on every laptop.
* *Wrong room code:* check the code on the host screen and connect again.
* *Room is full:* 8 laptops is the limit.
* *FL script is out of date:* reinstall it from Settings and click Update MIDI scripts.
* *"FL did not accept tempo":* that laptop's FL ignored the tempo change. The message names the laptop and its FL API number (also in Diagnostics). Update FL on that laptop and send me the number.
* Export diagnostic log saves a JSON report in Downloads (room code and host address are left out).

## Known limits

* Song mode loop regions are not tracked (pattern loops are). Each pass of a looped song region can cause one bar aligned correction.
* The installers aren't signed with Apple or Microsoft certificates, so each computer shows a one time warning (see Installing).
* Audio offset is set by hand. Measure it with the click test above.
