# Studio

Image generation and editing for the store page, the Crapletics listing and marketing, at https://studio.sxvij.com
behind Cloudflare Access. It runs in CT 114. The images come from Centre AI (CT 107), which runs SDXL on the GPU it
shares with Tickdeck's and Flipdeck's language model.

## What it does

- **Create:** text to image (SDXL) in nine shapes, six style presets, Draft (SDXL-Lightning, about 8-15 s) or Final
  (full SDXL, about 30 s), 1-8 at a time, with a fixed seed if you want the same picture again.
- **Edit part:** paint over an area and describe what goes there (SDXL inpainting).
- **Extend:** add picture around the edges (outpainting).
- **Upscale 2x or 4x** (Real-ESRGAN, on the GPU). Transparency is kept.
- **Remove background** (IS-Net, the rembg model, on this container's CPU, about 2 s).
- **Resize for:** App Store iPhone 6.9" and iPad 13", link preview, website hero, Instagram square, portrait and story,
  X header, Facebook cover, YouTube, app icon. Choose fill-and-crop or fit-inside.
- **Templates:** finished images with real, sharp text, rendered by headless Chromium at the exact size. Layouts:
  - Hero: a headline over a picture
  - Social: a picture with a headline band
  - App Store screenshot: a headline above an app screen
  - Product: a cut-out object on a gradient

  You set the headline, subline, button, colours, font and logo. Image models garble text, so text always goes
  through a template.
- **Library:** every result is kept with the prompt and settings that made it, and its parent (edits never overwrite).
  You can star, search, filter, download, delete and upload your own images.

## The GPU and Tickdeck

Centre AI runs one GPU job at a time and hands the 4 GB between the language model and the image models. Chat
requests (Tickdeck's news checks, Flipdeck) are interactive and go before studio jobs, which are background. A single
image delays a chat request by at most its own run time.

The studio adds one rule: **on market days, 9:30 AM to 4:00 PM ET, a batch of more than 3 images waits until the
close.** You can tick "run big batches during market hours anyway", or press "Run now" on the held job. Batches are
sent 2 images per request, so a chat request can get in between.

## Layout (CT 114, Debian 13)

| Path | What |
|---|---|
| `/opt/studio/app`, `/opt/studio/www` | this code (`REVISION` holds the deployed commit) |
| `/opt/studio/venv` | Python venv on top of Debian's packages, adding only `onnxruntime` and `numpy` |
| `/var/lib/studio/studio.db` | SQLite: items (library) and jobs |
| `/var/lib/studio/items`, `/var/lib/studio/thumbs` | PNGs and WebP thumbnails |
| `/var/lib/studio/models/isnet-general-use.onnx` | background-removal model (MD5-checked against rembg's pin) |
| `/etc/studio/env` | `AI_URL`, `AI_TOKEN` (Centre AI client `studio`: images allowed, background class), root:studio 0640 |
| `/etc/studio/gate.env` | Cloudflare Access team and AUD for the gate |

Units:

- `studio` on 127.0.0.1:8122 (user studio)
- `studio-gate`, the Access JWT check, on 127.0.0.1:8121 (same code as the other apps)
- `nginx` on 127.0.0.1:8120
- `cloudflared-studio` (token in `/etc/cloudflared/studio.env`)

Firewall (`server/nftables.conf`): nothing comes in. On the LAN it can reach only the router, Centre AI
(192.168.1.193:11500) and Wazuh.

## Security

- Two layers: Cloudflare Access at the edge, and the gate, which refuses anything from Cloudflare without a valid
  token. That fails closed until the AUD is set.
- Writes need JSON and the `X-Studio: 1` header, so other sites can't trigger them.
- The CSP allows no inline script.
- Template text is HTML-escaped. Chromium renders only local files and has its network off.

## Use from CT 200 (scripts, Claude)

The API answers locally without the gate. For example:

```sh
ssh pve 'pct exec 114 -- curl -s -X POST -H "Content-Type: application/json" -H "X-Studio: 1" \
  -d "{\"prompt\":\"...\",\"n\":2}" http://127.0.0.1:8120/api/generate'
```

Then poll `GET /api/jobs` and fetch `GET /api/items/<id>/image`.

## Deploy and test

```sh
./deploy.sh <reason>        # snapshot CT 114, run the tests in it, install, restart, check the gate
python3 tests/test_studio.py   # inside CT 114 (needs fastapi, Pillow, chromium); deploy.sh runs it
```

The first deploy also creates the user, folders, venv and model. The Centre AI token was passed once with
`--token` from stdin, and it never sits in the repo.

## Music tool (tools/music/compose.py, runs on CT 112 buildbox)

Background music for the casino apps (Crapletics, Jackletics, Roulettics), composed by rules and played by FluidSynth (pop chords too: bare names are triads, sus4, add9, slash chords like A/C#):
no AI model, so nothing to license and the same seed always gives the same music. Each style is six interchangeable
8-bar segments in one key and tempo; each starts on (or near) the home chord and ends on a turnaround back to it, with
3 s of tail. The apps (`js/music.js`) play them back to back on the beat in a random order, never the same segment
twice in a row, so the music goes on without looping the same way.

| Style | Band | Tempo |
|---|---|---|
| `lounge` (Lounge) | piano trio: rootless voicings, walking bass, brushes, a piano melody | 112, swung |
| `swing` (Vegas Swing) | piano, walking bass, jazz kit, muted trumpet, brass stabs | 150, swung |
| `bossa` (Bossa Nova) | nylon guitar in the bossa rhythm, flute, root-fifth bass, clave and shaker | 132, straight |
| `lofi` (Lo-fi Lounge) | electric piano, vibes, fingered bass, room kit, low-pass, vinyl crackle | 78, lazy swing |
| `latenight` (Late Night) | solo piano (root and rolled voicings, melody) with slow strings | 66 |
| `jpop` (J-Pop Rush) | J-pop progressions (royal road IV-V-iii-vi, Komuro vi-IV-V-I, Just the Two of Us, canon, a bVI-bVII-I lift): bright piano pulses, strings, picked bass in eighths, saw lead doubled by glockenspiel, overdriven power chords, 8-beat drums with tom fills | 150 |
| `dance` (Dance Floor) | house: four on the floor, claps, open hats on the off-beat, off-beat synth bass, polysynth stabs, a sixteenth arpeggio, square-lead hook, a snare build into each segment | 126 |
| `citypop` (City Pop) | 80s Tokyo: maj7 changes (Just the Two of Us), slap bass, electric piano and clean-guitar chops, brass hits, alto-sax lead, disco hats | 108 |

Each segment is mastered to -18 LUFS (the whole style measured together, one gain for all), peaks under -1 dBFS, MP3
96 kbps (about 1.5 MB a style); `preview.mp3` is two minutes played the way the apps play it, for listening only.

```sh
# on CT 200: copy the tool in, render, bring the files back into an app (not preview.mp3)
tar -C ~/studio/tools -cf - music | ssh pve "pct exec 112 -- su - builder -c 'mkdir -p ~/studio-tools && tar -C ~/studio-tools -xf -'"
ssh pve "pct exec 112 -- su - builder -c 'cd ~/studio-tools/music && python3 compose.py ~/music-out [style ...] [--seed 7]'"
ssh pve "pct exec 112 -- su - builder -c 'tar -C ~/music-out --exclude=preview.mp3 -cf - .'" | tar -C ~/craps/audio/music -xf -
```

Tools on CT 112 (installed 2026-11-13): fluidsynth 2.4, fluid-soundfont-gm (FluidR3_GM, MIT), sox, ffmpeg, numpy, and
`/opt/soundfonts/GeneralUser-GS.sf2` (S. Christian Collins, v2.0.3, sha256 9575028c…8cfe, from the author's GitHub;
license in `/opt/soundfonts/GeneralUser-GS-LICENSE.txt`: free for music, private or commercial). Not used: Meta's
MusicGen (its weights are non-commercial) and Stable Audio Open (gated download; free under $1M revenue): either could
be added later for ambience the rules can't make.

To change the music: edit a style's progressions (`progs`), band (`setup`: channel → program, volume, pan, reverb) or
parts in `compose()`; a new style also needs an entry in each app's `js/music.js` STYLES.
