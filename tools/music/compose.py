#!/usr/bin/env python3
"""Background music for the casino apps (Crapletics, Jackletics, Roulettics), composed by rules and played by
FluidSynth with a free soundfont.

Each style is a set of interchangeable 8-bar segments in one key and tempo. Every segment starts on (or near) the home
chord and ends on a turnaround that leads back to it, so an app can play them in any order, back to back on the beat:
each file has a few seconds of tail (reverb and decay) that rings under the next one. Endless music that never loops
the same way twice, from a few small files.

Run on CT 112 (buildbox) as builder:
    python3 compose.py OUT [style ...] [--seed 7] [--wav]
writes OUT/<style>/seg1.mp3 ... seg6.mp3, OUT/<style>/manifest.json and OUT/<style>/preview.mp3 (about two minutes
of segments played the way the apps play them, for listening; not for the apps).

Styles: lounge (jazz piano trio), swing (Vegas big-band swing), bossa (bossa nova), lofi (lo-fi lounge),
latenight (solo piano ballad with strings).

Needs fluidsynth, ffmpeg and numpy, and /opt/soundfonts/GeneralUser-GS.sf2 (S. Christian Collins, v2.0.3: "use without
restriction for your own music creation, private or commercial"; /opt/soundfonts/GeneralUser-GS-LICENSE.txt).
Deterministic: the same seed gives the same music.
"""
import argparse
import json
import math
import os
import random
import struct
import subprocess
import sys
import tempfile

import numpy as np

SF2 = os.environ.get('SF2', '/opt/soundfonts/GeneralUser-GS.sf2')
PPQ = 480
SR = 44100
TAIL = 3.0           # seconds rendered after the last bar (rings under the next segment)
TARGET_LUFS = -18.0  # every style at the same loudness; the apps set the volume
PEAK = 0.89          # -1 dBFS

# ---------------------------------------------------------------- harmony
PC = {'C': 0, 'C#': 1, 'Db': 1, 'D': 2, 'D#': 3, 'Eb': 3, 'E': 4, 'F': 5, 'F#': 6, 'Gb': 6, 'G': 7, 'G#': 8, 'Ab': 8,
      'A': 9, 'A#': 10, 'Bb': 10, 'B': 11}
# quality: (chord tones, colour tones for voicings and melody, chord scale, rootless voicing as intervals)
QUAL = {
    'maj7': ([0, 4, 7, 11], [14, 21], [0, 2, 4, 5, 7, 9, 11], [4, 7, 11, 14]),
    'maj9': ([0, 4, 7, 11], [14], [0, 2, 4, 7, 9, 11], [4, 7, 11, 14]),
    '6': ([0, 4, 7, 9], [14], [0, 2, 4, 5, 7, 9, 11], [4, 7, 9, 14]),
    'm7': ([0, 3, 7, 10], [14, 17], [0, 2, 3, 5, 7, 9, 10], [3, 7, 10, 14]),
    'm9': ([0, 3, 7, 10], [14], [0, 2, 3, 5, 7, 9, 10], [3, 7, 10, 14]),
    'm6': ([0, 3, 7, 9], [14], [0, 2, 3, 5, 7, 9, 11], [3, 7, 9, 14]),
    '7': ([0, 4, 7, 10], [14, 21], [0, 2, 4, 5, 7, 9, 10], [4, 9, 10, 14]),
    '9': ([0, 4, 7, 10], [14], [0, 2, 4, 5, 7, 9, 10], [4, 7, 10, 14]),
    '13': ([0, 4, 7, 10], [14, 21], [0, 2, 4, 7, 9, 10], [4, 9, 10, 14]),
    '7b9': ([0, 4, 7, 10], [13, 20], [0, 1, 4, 5, 7, 8, 10], [4, 8, 10, 13]),
    'm7b5': ([0, 3, 6, 10], [17], [0, 1, 3, 5, 6, 8, 10], [3, 6, 10, 12]),
    'dim7': ([0, 3, 6, 9], [], [0, 2, 3, 5, 6, 8, 9, 11], [0, 3, 6, 9]),
}


class Chord:
    def __init__(self, sym):
        name = sym[:2] if len(sym) > 1 and sym[1] in 'b#' else sym[:1]
        q = sym[len(name):] or 'maj7'
        if q not in QUAL:
            raise ValueError(f'chord {sym}: unknown quality {q}')
        self.sym, self.root = sym, PC[name]
        tones, colour, scale, voicing = QUAL[q]
        self.tones = [(self.root + i) % 12 for i in tones]
        self.colour = [(self.root + i) % 12 for i in colour]
        self.scale = [(self.root + i) % 12 for i in scale]
        self.voicing = [(self.root + i) % 12 for i in voicing]
        self.fifth = (self.root + (6 if q in ('m7b5', 'dim7') else 7)) % 12


def parse(prog):
    """'Fmaj7 | Gm7 C7 | ...' -> [(start beat, beats, Chord)] for 4/4 bars (two chords in a bar get two beats each)."""
    out = []
    for b, bar in enumerate(prog.split('|')):
        syms = bar.split()
        for i, s in enumerate(syms):
            n = 4 // len(syms)
            out.append((b * 4 + i * n, n, Chord(s)))
    return out


def chord_at(chords, beat):
    for st, du, c in chords:
        if st <= beat < st + du:
            return c
    return chords[-1][2]


def nearest(pc, ref, lo, hi):
    """The pitch of pitch class pc in [lo, hi] closest to ref."""
    cands = [p for p in range(lo, hi + 1) if p % 12 == pc]
    return min(cands, key=lambda p: (abs(p - ref), p))


def voice(c, prev, lo, hi, top=None):
    """A close-position rootless voicing of c with its lowest note in [lo, hi], moving as little as possible from prev."""
    pcs = c.voicing
    best = None
    for rot in range(len(pcs)):
        order = pcs[rot:] + pcs[:rot]
        for base in range(lo, hi + 1):
            if base % 12 != order[0]:
                continue
            v = [base]
            for pc in order[1:]:
                n = v[-1] + 1
                while n % 12 != pc:
                    n += 1
                v.append(n)
            if top and v[-1] > top:
                continue
            cost = sum(abs(a - b) for a, b in zip(v, prev)) if prev else abs(sum(v) / len(v) - (lo + hi) / 2 - 6)
            if best is None or cost < best[0]:
                best = (cost, v)
    return best[1]


# ---------------------------------------------------------------- notes, swing, MIDI
class Song:
    def __init__(self, bpm, swing, rng):
        self.bpm, self.swing, self.rng = bpm, swing, rng
        self.notes = []   # (channel, pitch, start beat, beats, velocity)

    def add(self, ch, pitch, start, dur, vel, human=0.012):
        r = self.rng
        start = max(0.0, start + (r.uniform(-human, human) if start > 0 else 0))
        vel = int(max(1, min(127, vel + r.randint(-5, 5))))
        self.notes.append((ch, int(pitch), start, max(0.05, dur), vel))


def swung(beat, s):
    """Swing the off-beat eighths: an eighth after the beat lands at s of the beat (0.5 straight, 0.667 triplet)."""
    b = math.floor(beat)
    f = beat - b
    return b + (f / 0.5 * s if f <= 0.5 else s + (f - 0.5) / 0.5 * (1 - s))


def vlq(n):
    out = [n & 0x7F]
    n >>= 7
    while n:
        out.insert(0, (n & 0x7F) | 0x80)
        n >>= 7
    return bytes(out)


def write_midi(path, song, setup, end_beat):
    """setup: {channel: (program, volume, pan, reverb)}; drums on channel 9 (program = drum kit)."""
    ev = []   # (tick, order, bytes)
    tempo = int(60_000_000 / song.bpm)
    ev.append((0, 0, b'\xff\x51\x03' + tempo.to_bytes(3, 'big')))
    for ch, (prog, vol, pan, rev) in setup.items():
        ev += [(0, 1, bytes([0xB0 | ch, 7, vol])), (0, 1, bytes([0xB0 | ch, 10, pan])), (0, 1, bytes([0xB0 | ch, 91, rev])),
               (0, 1, bytes([0xB0 | ch, 93, 0])), (0, 2, bytes([0xC0 | ch, prog]))]
    for ch, p, st, du, vel in song.notes:
        a = swung(st, song.swing)
        b = swung(st + du, song.swing) if ch != 9 else a + 0.2
        t0, t1 = round(a * PPQ), max(round(a * PPQ) + 1, round(b * PPQ) - 6)
        ev.append((t0, 4, bytes([0x90 | ch, p, vel])))
        ev.append((t1, 3, bytes([0x80 | ch, p, 0])))
    ev.append((round(end_beat * PPQ), 5, bytes([0xB0, 64, 0])))   # keeps the player running to the end of the tail
    ev.sort(key=lambda e: (e[0], e[1]))
    data, last = b'', 0
    for t, _, b in ev:
        data += vlq(t - last) + b
        last = t
    data += b'\x00\xff\x2f\x00'
    with open(path, 'wb') as f:
        f.write(b'MThd' + struct.pack('>IHHH', 6, 0, 1, PPQ) + b'MTrk' + struct.pack('>I', len(data)) + data)


# ---------------------------------------------------------------- parts
def walking_bass(song, chords, ch, home, lo=31, hi=50, vel=80):
    """Quarter notes: the root on each chord change, chord tones and steps in between, a chromatic or fifth approach
    into the next change (the segment's last beat leads into the home chord)."""
    r = song.rng
    total = chords[-1][0] + chords[-1][1]
    # Only real changes count: a chord held for two bars is walked through, not restarted on its root.
    starts, last_sym = {}, None
    for st, _, c in chords:
        if c.sym != last_sym:
            starts[st] = c
        last_sym = c.sym
    prev = None
    for beat in range(total):
        c = chord_at(chords, beat)
        nxt = starts.get(beat + 1)
        nxt_root = (nxt.root if nxt else home) if (beat + 1 in starts or beat + 1 == total) else None
        if beat in starts:
            p = nearest(c.root, prev if prev else 38, lo, hi)
            if prev and abs(p - prev) > 7 and lo <= p + (12 if p < prev else -12) <= hi:
                p += 12 if p < prev else -12
        elif beat % 4 == 0 and r.random() < .6:                                # a held chord's new bar: its 3rd or 5th
            p = nearest(r.choice(c.tones[1:3]), prev, lo, hi)
            if p == prev:
                p = nearest(c.tones[1], prev + 3, lo, hi)
        elif nxt_root is not None:
            tgt = nearest(nxt_root, prev, lo + 1, hi - 1)
            opts = [tgt - 1, tgt + 1, tgt + 7 - 12 if tgt + 7 - 12 >= lo else tgt + 7]
            p = r.choice([o for o in opts if o != prev and lo <= o <= hi] or [tgt + 1])
        else:
            ahead = next((s for s in sorted(starts) if s > beat), total)
            tgt_pc = starts[ahead].root if ahead in starts else home
            tgt = nearest(tgt_pc, prev, lo, hi)
            pool = [n for n in range(prev - 5, prev + 6) if lo <= n <= hi and n != prev and n % 12 in set(c.tones + c.scale)]
            pool.sort(key=lambda n: (abs((tgt - n)) + (0 if n % 12 in c.tones else 1.5) + r.random() * 2))
            p = pool[0] if pool else prev
        song.add(ch, p, beat, 0.95, vel + (4 if beat % 2 == 0 else 0), human=0.006)
        prev = p


def comp(song, chords, ch, lo, hi, top, patterns, vel=58, dur_scale=1.0):
    """Chords on a rhythm from `patterns` (beats within a chord's span), voice-led."""
    r = song.rng
    prev = None
    for st, du, c in chords:
        v = voice(c, prev, lo, hi, top)
        prev = v
        pat = r.choice([p for p in patterns if all(o < du for o, _ in p)] or [[(0, du * 0.9)]])
        for off, d in pat:
            for i, p in enumerate(v):
                song.add(ch, p, st + off + i * 0.004, d * dur_scale, vel - (6 if i == 0 else 0))


CELLS = {   # melody rhythms within a bar: (onset beat, beats)
    'busy': [[(0, 1), (1, .5), (1.5, .5), (2, 1.5)], [(.5, .5), (1, .5), (1.5, .5), (2, 1), (3, 1)],
             [(0, 1.5), (1.5, .5), (2, .5), (2.5, 1.5)], [(1, .5), (1.5, .5), (2, .5), (2.5, .5), (3, 1)],
             [(0, .5), (.5, .5), (1, 1), (2.5, .5), (3, 1)], [(0, 2), (2.5, .5), (3, 1)]],
    'calm': [[(0, 2), (2, 2)], [(0, 3), (3, 1)], [(.5, 1.5), (2, 2)], [(0, 1), (1, 1), (2, 2)],
             [(1, 1), (2, 1), (3, 1)], [(0, 1.5), (1.5, .5), (2, 2)]],
    'end': [[(0, 3.5)], [(0, 1), (1, 2.5)], [(0, .5), (.5, 3)], [(0, 2), (2, 1.5)]],
}


def melody(song, chords, ch, lo, hi, density, play=0.75, vel=72):
    """Two-bar phrases (call, then an answer that settles on a chord tone), some left out so it breathes; a phrase
    may repeat the last motif, moved to fit the new chords."""
    r = song.rng
    total = chords[-1][0] + chords[-1][1]
    mid = (lo + hi) // 2
    prev, motif, before = mid, None, None
    for ph in range(0, total, 8):
        if r.random() > play:
            continue
        up = 1 if prev < mid - 3 else -1 if prev > mid + 3 else r.choice([1, -1])   # head back toward the middle
        for bar in range(2):
            b0 = ph + bar * 4
            if b0 >= total:
                break
            reuse = bar == 0 and motif and r.random() < 0.5
            cell = motif[0] if reuse else r.choice(CELLS['end'] if bar == 1 else CELLS[density])
            new = []
            for k, (on, d) in enumerate(cell):
                beat = b0 + on
                c = chord_at(chords, beat)
                last = bar == 1 and k == len(cell) - 1
                strong = on in (0, 2) or d >= 1.5 or last
                if reuse and k < len(motif[1]):
                    want = prev + motif[1][k]
                    pool = [p for p in range(lo, hi + 1) if p % 12 in c.scale]
                    p = min(pool, key=lambda n: abs(n - want))
                else:
                    pcs = set(c.tones[1:] + c.colour) if strong else set(c.scale)
                    pool = [n for n in range(lo, hi + 1) if n % 12 in pcs]
                    # a step or two in the phrase's direction, pulled gently back toward the middle of the range;
                    # the same note again is rare, and never a third time in a row
                    aim = prev + up * (2 if bar == 0 else -2) + (mid - prev) * .2
                    w = [math.exp(-abs(n - aim) / 2.2) * (0.08 if n == prev else 1) * (0 if n == prev == before else 1)
                         * (0.05 if abs(n - prev) > 7 else 1) + 1e-9 for n in pool]
                    p = r.choices(pool, w)[0]
                new.append(p - prev)
                before = prev
                song.add(ch, p, beat, d * (0.92 if not last else 1.0), vel + (6 if strong else 0) + r.randint(-4, 4), human=0.015)
                prev = p
            if bar == 0:
                motif = (cell, new)


def drums_swing(song, total, brushes, backbeat=False, fill=True):
    r = song.rng
    for beat in range(total):
        song.add(9, 51, beat, .2, 62 if beat % 2 else 52, human=.004)           # ride
        if beat % 2 == 1:
            song.add(9, 51, beat + .5, .2, 44, human=.004)                     # the swung "a" before 2 and 4
            song.add(9, 44, beat, .2, 50, human=.004)                          # hi-hat foot on 2 and 4
            if brushes:
                song.add(9, 38, beat, .2, 34, human=.006)                      # brush tap
            elif backbeat:
                song.add(9, 38, beat, .2, 40, human=.006)
        if beat % 4 in (0, 2):
            song.add(9, 36, beat, .2, 26, human=.004)                          # feathered bass drum
        if not brushes and r.random() < 0.12:
            song.add(9, 38, beat + r.choice([.5, 1.5 if beat % 4 < 2 else .5]), .2, 30)   # snare comping
    if brushes:
        for bar in range(0, total, 4):
            song.add(9, 40, bar, 3.8, 30, human=0)                             # brush swirl under each bar
    if fill:
        last = total - 4
        song.add(9, 38, last + 3, .2, 42 if brushes else 52)
        song.add(9, 38, last + 3.5, .2, 48 if brushes else 60)
        if not brushes:
            song.add(9, 36, last + 3.5, .2, 50)


def drums_bossa(song, total):
    for bar in range(0, total, 4):
        two = (bar // 4) % 2
        for on in ([0, 1.5, 3] if two == 0 else [1, 2.5]):
            song.add(9, 37, bar + on, .2, 58, human=.005)                       # cross-stick clave
        for e in range(8):
            song.add(9, 70, bar + e * .5, .2, 34 if e % 2 == 0 else 24, human=.005)   # shaker
            song.add(9, 42, bar + e * .5, .2, 26 if e % 2 == 0 else 18, human=.005)
        for on, v in ((0, 48), (1.5, 30), (2, 46), (3.5, 30)):
            song.add(9, 36, bar + on, .2, v, human=.004)


def drums_lofi(song, total):
    r = song.rng
    for bar in range(0, total, 4):
        for on, v in ((0, 70), (2.5, 58)) + (((1.75, 40),) if r.random() < .4 else ()):
            song.add(9, 36, bar + on, .2, v, human=.01)
        for on in (1, 3):
            song.add(9, 38, bar + on, .2, 62, human=.012)
        if r.random() < .5:
            song.add(9, 38, bar + 3.75, .2, 22)                                 # ghost note
        for e in range(8):
            song.add(9, 46 if (e == 7 and r.random() < .3) else 42, bar + e * .5, .2, 44 if e % 2 == 0 else 30, human=.012)


def bossa_bass(song, chords, ch, home, vel=78):
    total = chords[-1][0] + chords[-1][1]
    prev = 38
    for bar in range(0, total, 4):
        for on, d, kind in ((0, 1.4, 'root'), (1.5, .45, 'fifth'), (2, 1.4, 'fifth'), (3.5, .45, 'next')):
            c = chord_at(chords, bar + on)
            if kind == 'next':                                                  # a pickup into the next bar's root
                pc = chord_at(chords, bar + 4).root if bar + 4 < total else home
            else:
                pc = c.root if kind == 'root' else c.fifth
            p = nearest(pc, prev, 33, 50)
            song.add(ch, p, bar + on, d, vel - (8 if on % 1 else 0), human=.006)
            prev = p


def lofi_bass(song, chords, ch, vel=82):
    for st, du, c in chords:
        p = nearest(c.root, 38, 31, 46)
        song.add(ch, p, st, min(du, 1.5) - .1, vel, human=.01)
        if du >= 4:
            song.add(ch, p, st + 2.5, .4, vel - 10, human=.01)
            song.add(ch, nearest(c.fifth, p, 31, 50), st + 3, .9, vel - 6, human=.01)


def ballad_piano(song, chords, ch, home):
    """Left hand: the root, then the chord's colour above it; right hand: the melody (melody())."""
    prev = None
    for st, du, c in chords:
        root = nearest(c.root, 40, 33, 47)
        song.add(ch, root, st, du - .1, 56, human=.01)
        v = voice(c, prev, 50, 60, 70)
        prev = v
        for i, p in enumerate(v):
            song.add(ch, p, st + .5 + i * .09, du - .7, 40 - i * 2, human=.01)   # rolled, gently


def pad(song, chords, ch, vel=44):
    prev = None
    for st, du, c in chords:
        v = voice(c, prev, 55, 64, 79)
        prev = v
        for p in v[:3]:
            song.add(ch, p, st, du - .05, vel, human=0)


# ---------------------------------------------------------------- styles
SWING_COMP = [[(0, .9), (2.5, .4)], [(1.5, .5), (3.5, .4)], [(0, 1.4), (1.5, .4)], [(.5, .4), (2, .9)], [(1, .5), (2.5, .5)],
              [(0, .5)], [(0, 1.8)]]
STYLES = {
    'lounge': dict(
        name='Lounge', blurb='A jazz piano trio with brushes', bpm=112, swing=.64, home='F',
        progs=['Fmaj7 | Fmaj7 | Gm7 | C7 | Am7 | D7 | Gm7 | Gm7 C7',
               'Fmaj7 | Bb7 | Am7 | D7b9 | Gm7 | C7 | Am7 D7 | Gm7 C7',
               'Fmaj7 D7 | Gm7 C7 | Am7 Abdim7 | Gm7 C7 | Fmaj7 | Cm7 F7 | Bbmaj7 | Gm7 C7',
               'Fmaj7 | Ebm7 Ab7 | Dbmaj7 | Gm7 C7 | Fmaj7 | Bbm7 Eb7 | Am7 D7 | Gm7 C7',
               'Bbmaj7 | Bbm7 Eb7 | Am7 | D7 | Gm7 | C7 | Fmaj7 D7 | Gm7 C7',
               'Fmaj7 | Am7b5 D7b9 | Gm7 | Bbm7 Eb7 | Am7 | D7 | Gm7 | Gm7 C7'],
        setup={0: (0, 82, 52, 45), 1: (0, 92, 74, 50), 2: (32, 104, 64, 22), 9: (40, 86, 64, 40)},
    ),
    'swing': dict(
        name='Vegas Swing', blurb='Uptempo swing with a muted trumpet and brass', bpm=150, swing=.62, home='Bb',
        progs=['Bb6 | G7 | Cm7 | F7 | Bb6 | G7 | Cm7 | Cm7 F7',
               'Bb6 | Bb7 | Eb6 | Edim7 | Bb6 G7 | Cm7 F7 | Dm7 G7 | Cm7 F7',
               'Ebmaj7 | Ebm7 Ab7 | Dm7 | G7 | Cm7 | F7 | Bb6 | Cm7 F7',
               'D7 | D7 | G7 | G7 | C7 | C7 | F7 | Cm7 F7',
               'Bb6 G7 | Cm7 F7 | Bb6 G7 | Cm7 F7 | Fm7 Bb7 | Eb6 Edim7 | Bb6 G7 | Cm7 F7',
               'Bb6 | Gm7 | Cm7 | F7 | Dm7 | Db7 | Cm7 | Cm7 F7'],
        setup={0: (0, 78, 48, 40), 1: (59, 88, 78, 55), 3: (61, 70, 60, 50), 2: (32, 104, 64, 22), 9: (32, 92, 64, 38)},
    ),
    'bossa': dict(
        name='Bossa Nova', blurb='Nylon guitar, flute and a soft clave', bpm=132, swing=.5, home='D',
        progs=['Dm9 | Dm9 | Em7b5 | A7b9 | Dm9 | Dm9 | Em7b5 | A7b9',
               'Gm9 | C9 | Fmaj7 | Bbmaj7 | Em7b5 | A7b9 | Dm9 | Em7b5 A7b9',
               'Dm9 | G9 | Dm9 | G9 | Gm9 | C9 | Fmaj7 | Em7b5 A7b9',
               'Bbmaj7 | Bbm6 | Am7 | D7b9 | Gm9 | C9 | Fmaj7 | Em7b5 A7b9',
               'Dm9 | Bbmaj7 | Gm9 | A7b9 | Dm9 | Bbmaj7 | Em7b5 | A7b9',
               'Fmaj7 | Fmaj7 | Gm9 | C9 | Am7 | D7b9 | Gm9 | Em7b5 A7b9'],
        setup={0: (24, 100, 54, 40), 1: (73, 78, 76, 62), 2: (32, 98, 64, 22), 9: (0, 78, 64, 34)},
    ),
    'lofi': dict(
        name='Lo-fi Lounge', blurb='Electric piano, vibes and a lazy beat', bpm=78, swing=.58, home='Eb',
        progs=['Ebmaj9 | Cm9 | Fm9 | Bb13 | Ebmaj9 | Cm9 | Fm9 | Bb13',
               'Abmaj9 | Gm7 | Fm9 | Bb13 | Abmaj9 | Gm7 Cm9 | Fm9 | Bb13',
               'Ebmaj9 | Dm7b5 G7b9 | Cm9 | Bbm9 Eb9 | Abmaj9 | Abm6 | Gm7 Cm9 | Fm9 Bb13',
               'Cm9 | Cm9 | Abmaj9 | Abmaj9 | Fm9 | Gm7 | Abmaj9 | Bb13',
               'Ebmaj9 | Ebmaj9 | Abmaj9 | Abm6 | Gm7 | Cm9 | Fm9 | Bb13',
               'Fm9 | Bb13 | Gm7 | Cm9 | Fm9 | Bb13 | Ebmaj9 | Fm9 Bb13'],
        setup={0: (4, 98, 58, 58), 1: (11, 74, 74, 66), 2: (33, 104, 64, 20), 9: (8, 90, 64, 30)},
    ),
    'latenight': dict(
        name='Late Night', blurb='Solo piano and strings, slow and quiet', bpm=66, swing=.55, home='C',
        progs=['Cmaj7 | Am7 | Dm7 | G7 | Em7 | A7 | Dm7 | Dm7 G7',
               'Fmaj7 | Fm6 | Em7 | A7b9 | Dm7 | G7 | Cmaj7 A7 | Dm7 G7',
               'Cmaj7 | C7 | Fmaj7 | Bb7 | Em7 | A7 | Dm7 G7 | Dm7 G7',
               'Am7 | Am7 | Dm7 | G7 | Cmaj7 | Fmaj7 | Bm7b5 E7b9 | Dm7 G7',
               'Cmaj7 | Ebdim7 | Dm7 | G7 | Cmaj7 | Bbm7 Eb7 | Abmaj7 | Dm7 G7',
               'Fmaj7 | Em7 | Dm7 | Cmaj7 | Bm7b5 | E7b9 | Am7 D7 | Dm7 G7'],
        setup={0: (0, 104, 60, 62), 3: (49, 58, 70, 72)},
    ),
}


def compose(style, idx, seed):
    st = STYLES[style]
    rng = random.Random(f'{style}-{idx}-{seed}')
    song = Song(st['bpm'], st['swing'], rng)
    chords = parse(st['progs'][idx])
    total = chords[-1][0] + chords[-1][1]
    home = PC[st['home']]
    if style == 'lounge':
        comp(song, chords, 0, 48, 58, 70, SWING_COMP, vel=54)
        walking_bass(song, chords, 2, home)
        melody(song, chords, 1, 65, 84, 'busy', play=.7, vel=70)
        drums_swing(song, total, brushes=True)
    elif style == 'swing':
        comp(song, chords, 0, 48, 58, 70, SWING_COMP, vel=56)
        walking_bass(song, chords, 2, home, vel=86)
        melody(song, chords, 1, 62, 80, 'busy', play=.8, vel=74)
        prev = None
        for i, (s, du, c) in enumerate(chords):                                 # brass stabs
            v = voice(c, prev, 60, 68, 80)
            prev = v
            for off in ([1.5, 3.5] if du == 4 and i % 2 else [0] if du == 2 and rng.random() < .5 else []):
                for p in v:
                    song.add(3, p, s + off, .35, 66)
        drums_swing(song, total, brushes=False, backbeat=True)
        song.add(9, 49, 0, .5, 54)                                              # a crash to start each segment
    elif style == 'bossa':
        pats = [[(0, 1.0), (1.5, 1.0), (3, .75)], [(1, 1.0), (2.5, 1.4)]]
        prev = None
        for b in range(0, total, 4):                                            # guitar: the two-bar bossa rhythm
            for off, d in pats[(b // 4) % 2]:
                c = chord_at(chords, b + off)
                v = voice(c, prev, 50, 58, 70)
                prev = v
                for i, p in enumerate(v):
                    song.add(0, p, b + off + i * .012, d, 54 - (4 if i == 0 else 0), human=.006)
        bossa_bass(song, chords, 2, home)
        melody(song, chords, 1, 67, 86, 'calm', play=.75, vel=66)
        drums_bossa(song, total)
    elif style == 'lofi':
        prev = None
        for s, du, c in chords:                                                 # electric piano: long chords, a push
            v = voice(c, prev, 51, 60, 74)
            prev = v
            for i, p in enumerate(v):
                song.add(0, p, s + i * .02, min(du, 3.5) - .1, 50, human=.01)
            if du == 4 and rng.random() < .5:
                for p in v[1:]:
                    song.add(0, p, s + 2.5, 1.2, 40, human=.01)
        lofi_bass(song, chords, 2)
        melody(song, chords, 1, 67, 84, 'calm', play=.6, vel=62)
        drums_lofi(song, total)
    elif style == 'latenight':
        ballad_piano(song, chords, 0, home)
        melody(song, chords, 0, 67, 86, 'calm', play=.85, vel=60)
        pad(song, chords, 3)
    return song, total, st


def run(cmd, data=None):
    p = subprocess.run(cmd, input=data, capture_output=True)
    if p.returncode:
        sys.exit(f'{cmd[0]} failed: {p.stderr.decode(errors="replace")[-800:]}')
    return p


def render(song, total, st, tmp, name):
    mid, wav = os.path.join(tmp, name + '.mid'), os.path.join(tmp, name + '.wav')
    tail_beats = TAIL * st['bpm'] / 60
    write_midi(mid, song, st['setup'], total + tail_beats + 1)
    run(['fluidsynth', '-ni', '-q', '-g', '0.45', '-r', str(SR), '-O', 'float', '-T', 'wav', '-F', wav,
         '-o', 'synth.reverb.room-size=0.62', '-o', 'synth.reverb.damp=0.35', '-o', 'synth.reverb.width=0.9',
         '-o', 'synth.reverb.level=0.75', '-o', 'synth.chorus.active=0', SF2, mid])
    filt = 'highpass=f=38,lowpass=f=6800' if st is STYLES['lofi'] else 'highpass=f=32'
    raw = run(['ffmpeg', '-v', 'error', '-i', wav, '-af', filt, '-f', 'f32le', '-ac', '2', '-ar', str(SR), '-']).stdout
    x = np.frombuffer(raw, dtype=np.float32).reshape(-1, 2).copy()
    n = round((total * 60 / st['bpm'] + TAIL) * SR)
    x = np.pad(x, ((0, max(0, n - len(x))), (0, 0)))[:n]
    f = round(1.2 * SR)                                                         # the tail fades out
    x[-f:] *= (0.5 + 0.5 * np.cos(np.linspace(0, math.pi, f)))[:, None]
    return x


def vinyl(x, rng):
    """Lo-fi: a little crackle and hiss."""
    g = np.random.default_rng(rng.randint(0, 2**31))
    out = x.copy()
    hiss = np.cumsum(g.normal(0, 1, len(x)))
    hiss = (hiss - np.convolve(hiss, np.ones(400) / 400, 'same')) * 0.00012    # after the loudness is set: quiet
    out += hiss[:, None]
    for pos in g.integers(0, len(x) - 60, int(len(x) / SR * 5)):
        k = g.uniform(.003, .012) * g.choice([-1, 1]) * np.exp(-np.arange(40) / 6)
        out[pos:pos + 40] += k[:, None]
    return out


def loudness(x):
    p = run(['ffmpeg', '-nostats', '-f', 'f32le', '-ar', str(SR), '-ac', '2', '-i', '-', '-af', 'ebur128', '-f', 'null', '-'], x.astype(np.float32).tobytes())
    lines = [ln for ln in p.stderr.decode().splitlines() if ln.strip().startswith('I:')]
    return float(lines[-1].split()[1])


def encode(x, path, kbps):
    run(['ffmpeg', '-v', 'error', '-y', '-f', 'f32le', '-ar', str(SR), '-ac', '2', '-i', '-', '-c:a', 'libmp3lame', '-b:a', f'{kbps}k', path],
        np.clip(x, -1, 1).astype(np.float32).tobytes())


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    ap.add_argument('out')
    ap.add_argument('styles', nargs='*', default=list(STYLES))
    ap.add_argument('--seed', type=int, default=7)
    ap.add_argument('--wav', action='store_true', help='also keep WAV files')
    a = ap.parse_args()
    for style in a.styles:
        st = STYLES[style]
        d = os.path.join(a.out, style)
        os.makedirs(d, exist_ok=True)
        with tempfile.TemporaryDirectory() as tmp:
            segs = []
            for i in range(len(st['progs'])):
                song, total, _ = compose(style, i, a.seed)
                segs.append((render(song, total, st, tmp, f'seg{i + 1}'), song.rng))
            beats = len(st['progs'][0].split('|')) * 4
            step = round(beats * 60 / st['bpm'] * SR)
            # the preview: segments the way the apps play them (random order, no repeat twice in a row, on the beat)
            r = random.Random(a.seed)
            order, last = [], None
            while len(order) * step < 120 * SR:
                k = r.choice([j for j in range(len(segs)) if j != last])
                order.append(k)
                last = k
            def mix(xs):
                out = np.zeros((step * len(order) + len(xs[0]), 2), np.float32)
                for j, k in enumerate(order):
                    out[j * step:j * step + len(xs[k])] += xs[k]
                return out
            clean, rngs = [x for x, _ in segs], [g for _, g in segs]
            prev = mix(clean)
            lufs = loudness(prev)
            gain = min(10 ** ((TARGET_LUFS - lufs) / 20), PEAK / max(np.abs(prev).max(), max(np.abs(x).max() for x in clean)))
            segs = [x * gain for x in clean]
            if style == 'lofi':                                                 # crackle and hiss on top, at a set level
                segs = [vinyl(x, g) for x, g in zip(segs, rngs)]
            prev = mix(segs)
            gain = 1.0
            files = []
            for i, x in enumerate(segs):
                name = f'seg{i + 1}.mp3'
                encode(x * gain, os.path.join(d, name), 96)
                if a.wav:
                    run(['ffmpeg', '-v', 'error', '-y', '-f', 'f32le', '-ar', str(SR), '-ac', '2', '-i', '-', '-c:a', 'pcm_s16le',
                         os.path.join(d, f'seg{i + 1}.wav')], np.clip(x * gain, -1, 1).astype(np.float32).tobytes())
                files.append(name)
            encode(prev * gain, os.path.join(d, 'preview.mp3'), 128)
            man = {'style': style, 'name': st['name'], 'blurb': st['blurb'], 'bpm': st['bpm'], 'beats': beats,
                   'len': beats * 60 / st['bpm'], 'tail': TAIL, 'segments': files,
                   'lufs': round(loudness(prev), 1), 'seed': a.seed}
            with open(os.path.join(d, 'manifest.json'), 'w') as f:
                json.dump(man, f, indent=1)
            print(f'{style}: {len(files)} segments of {man["len"]:.1f} s (+{TAIL:.0f} s tail), {man["lufs"]} LUFS, '
                  f'peak {20 * math.log10(max(np.abs(prev).max() * gain, 1e-9)):.1f} dBFS')


if __name__ == '__main__':
    main()
