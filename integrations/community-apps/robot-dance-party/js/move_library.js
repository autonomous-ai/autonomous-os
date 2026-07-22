// move_library.js — Dance move definitions for Autonomous OS robots
//
// Equivalent to duo's move_library.js + EIGHT_BEAT_SEQUENCES in beat_bandit.
// Duo defines moves as 4x4 head matrices + antenna angles + body yaw.
// We define moves as servo aim directions + LED effects, since Autonomous OS
// uses a simpler command API (/servo/aim, /led/solid) instead of raw IK.
//
// Moves are organized as 8-beat sequences at three energy levels,
// matching duo's high/medium/low sequence_assignments pattern.

// Each beat in a sequence defines what the robot does on that beat.
// direction: servo aim direction (up/down/left/right/center)
// duration: ms for the servo move
// led: [R, G, B] color to flash on this beat (null = keep current)
// antenna: optional emotion to trigger (sparse, not every beat)

const SEQUENCES = {
  // High energy — fast, full-range moves, bright colors
  high: [
    [
      { direction: 'left',   duration: 200, led: [255, 0, 100] },
      { direction: 'right',  duration: 200, led: [100, 0, 255] },
      { direction: 'up',     duration: 150, led: [255, 50, 0] },
      { direction: 'down',   duration: 150, led: [0, 200, 255] },
      { direction: 'right',  duration: 200, led: [255, 0, 100] },
      { direction: 'left',   duration: 200, led: [100, 0, 255] },
      { direction: 'up',     duration: 150, led: [255, 200, 0] },
      { direction: 'center', duration: 300, led: [255, 255, 255], emotion: 'happy' },
    ],
    [
      { direction: 'up',     duration: 150, led: [255, 0, 0] },
      { direction: 'down',   duration: 150, led: [0, 0, 255] },
      { direction: 'up',     duration: 150, led: [255, 0, 0] },
      { direction: 'down',   duration: 150, led: [0, 0, 255] },
      { direction: 'left',   duration: 200, led: [255, 100, 0] },
      { direction: 'right',  duration: 200, led: [0, 255, 100] },
      { direction: 'left',   duration: 200, led: [255, 100, 0] },
      { direction: 'center', duration: 250, led: [255, 255, 255], emotion: 'surprised' },
    ],
    [
      { direction: 'right',  duration: 180, led: [200, 0, 255] },
      { direction: 'left',   duration: 180, led: [0, 255, 200] },
      { direction: 'up',     duration: 150, led: [255, 255, 0] },
      { direction: 'right',  duration: 180, led: [200, 0, 255] },
      { direction: 'left',   duration: 180, led: [0, 255, 200] },
      { direction: 'down',   duration: 200, led: [255, 0, 50] },
      { direction: 'up',     duration: 150, led: [255, 255, 0] },
      { direction: 'center', duration: 300, led: [255, 200, 255], emotion: 'happy' },
    ],
  ],

  // Medium energy — moderate speed, warm colors
  medium: [
    [
      { direction: 'left',   duration: 300, led: [180, 80, 0] },
      { direction: 'center', duration: 300, led: [120, 60, 0] },
      { direction: 'right',  duration: 300, led: [180, 80, 0] },
      { direction: 'center', duration: 300, led: [120, 60, 0] },
      { direction: 'up',     duration: 350, led: [200, 100, 0] },
      { direction: 'center', duration: 300, led: [140, 70, 0] },
      { direction: 'down',   duration: 350, led: [200, 100, 0] },
      { direction: 'center', duration: 400, led: [160, 80, 20] },
    ],
    [
      { direction: 'up',     duration: 350, led: [0, 150, 200] },
      { direction: 'left',   duration: 300, led: [0, 120, 180] },
      { direction: 'down',   duration: 350, led: [0, 150, 200] },
      { direction: 'right',  duration: 300, led: [0, 120, 180] },
      { direction: 'up',     duration: 350, led: [50, 180, 220] },
      { direction: 'center', duration: 300, led: [0, 100, 160] },
      { direction: 'left',   duration: 300, led: [50, 180, 220] },
      { direction: 'center', duration: 400, led: [0, 140, 200], emotion: 'curious' },
    ],
    [
      { direction: 'right',  duration: 300, led: [150, 0, 200] },
      { direction: 'up',     duration: 300, led: [120, 0, 160] },
      { direction: 'left',   duration: 300, led: [150, 0, 200] },
      { direction: 'down',   duration: 300, led: [120, 0, 160] },
      { direction: 'right',  duration: 300, led: [180, 50, 220] },
      { direction: 'center', duration: 350, led: [100, 0, 140] },
      { direction: 'up',     duration: 300, led: [180, 50, 220] },
      { direction: 'center', duration: 400, led: [140, 30, 180] },
    ],
  ],

  // Low energy — slow, gentle sway, cool colors
  low: [
    [
      { direction: 'left',   duration: 500, led: [0, 40, 80] },
      { direction: 'center', duration: 500, led: [0, 30, 60] },
      { direction: 'right',  duration: 500, led: [0, 40, 80] },
      { direction: 'center', duration: 500, led: [0, 30, 60] },
      { direction: 'left',   duration: 500, led: [0, 50, 90] },
      { direction: 'center', duration: 500, led: [0, 30, 60] },
      { direction: 'right',  duration: 500, led: [0, 50, 90] },
      { direction: 'center', duration: 600, led: [0, 40, 80], emotion: 'sleepy' },
    ],
    [
      { direction: 'up',     duration: 600, led: [30, 0, 60] },
      { direction: 'center', duration: 500, led: [20, 0, 40] },
      { direction: 'down',   duration: 600, led: [30, 0, 60] },
      { direction: 'center', duration: 500, led: [20, 0, 40] },
      { direction: 'up',     duration: 600, led: [40, 0, 70] },
      { direction: 'center', duration: 500, led: [20, 0, 40] },
      { direction: 'down',   duration: 600, led: [40, 0, 70] },
      { direction: 'center', duration: 700, led: [30, 0, 50] },
    ],
  ],
};

// Round-robin indices per energy level (like duo's _lastSequenceIdx)
const _seqIndex = { high: 0, medium: 0, low: 0 };

// Select the next sequence for a given energy level (round-robin to avoid repeats)
export function selectSequence(energyLevel) {
  const pool = SEQUENCES[energyLevel] || SEQUENCES.medium;
  const idx = _seqIndex[energyLevel] || 0;
  const seq = pool[idx % pool.length];
  _seqIndex[energyLevel] = (idx + 1) % pool.length;
  return seq;
}

// Get the move for a specific beat within the current sequence
export function getMoveAtBeat(sequence, beatIndex) {
  return sequence[beatIndex % sequence.length];
}

// Classify energy level from raw energy value (0-255 scale)
// Similar to duo's sequence_assignments: high/medium/low
export function classifyEnergy(energy) {
  if (energy > 140) return 'high';
  if (energy > 80) return 'medium';
  return 'low';
}

// Blend LED color based on frequency bands (continuous, not just on beats)
// Maps bass→red, mid→green, high→blue (spectral mapping)
export function spectrumColor(bass, mid, high) {
  return [
    Math.floor(Math.min(bass * 1.6, 255)),
    Math.floor(Math.min(mid * 0.9, 255)),
    Math.floor(Math.min(high * 1.3, 255)),
  ];
}

// Brighten a color for beat flash (like duo's energy-driven brightness)
export function flashColor(color, intensity) {
  const boost = 60 + intensity * 80;
  return color.map(c => Math.min(c + boost, 255));
}

// Dim a color for between-beat ambient (continuous spectrum glow)
export function dimColor(color, factor = 0.35) {
  return color.map(c => Math.floor(c * factor));
}
