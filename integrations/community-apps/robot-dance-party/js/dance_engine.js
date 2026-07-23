// dance_engine.js — Maps audio analysis to robot commands
//
// Equivalent to duo's BeatBandit._tick() control loop:
//   audio.currentTime → beatIndex → blockIndex → energy level
//   → select sequence → interpolate to target → send to robot
//
// Our pipeline:
//   audioEngine.analyze() → energy level → select sequence
//   → on beat: pick move → send LED + servo + emotion to robot

import { selectSequence, getMoveAtBeat, classifyEnergy, spectrumColor, flashColor, dimColor } from './move_library.js';

export class DanceEngine extends EventTarget {
  constructor(audioEngine, connection, safety) {
    super();
    this.audio = audioEngine;
    this.robot = connection;
    this.safety = safety;

    // Dance state (like duo's _currentBlock, _currentSequence)
    this._currentSequence = null;
    this._currentEnergy = 'low';
    this._beatCount = 0;        // Global beat counter
    this._blockBeat = 0;        // Beat within current 8-beat block
    this._lastBlockEnergy = '';  // Detect energy level changes

    // Toggles (controlled by UI switches)
    this.ledEnabled = true;
    this.servoEnabled = true;
    this.emotionEnabled = false;

    // Animation loop
    this._frameId = null;
    this._running = false;

    // Bind for rAF
    this._tick = this._tick.bind(this);
  }

  start() {
    if (this._running) return;
    this._running = true;
    this._beatCount = 0;
    this._blockBeat = 0;
    this._currentSequence = selectSequence('medium');
    this._frameId = requestAnimationFrame(this._tick);
    this._emit('started');
  }

  stop() {
    this._running = false;
    if (this._frameId) {
      cancelAnimationFrame(this._frameId);
      this._frameId = null;
    }
    // Reset LED
    this.robot.ledEffectStop().catch(() => {});
    this._emit('stopped');
  }

  // --- Main control loop (like duo's _tick at 50Hz) ---
  // We run at display framerate (~60Hz) but throttle robot commands via safety

  _tick(timestamp) {
    if (!this._running) return;
    this._frameId = requestAnimationFrame(this._tick);

    // Get audio analysis for this frame
    const analysis = this.audio.analyze(timestamp);
    const { bass, mid, high, energy, isBeat, bpm } = analysis;

    // Classify energy level (like duo's sequence_assignments)
    const energyLevel = classifyEnergy(energy);

    // On energy level change: select new sequence (like duo's _selectSequence)
    if (energyLevel !== this._lastBlockEnergy) {
      this._currentSequence = selectSequence(energyLevel);
      this._blockBeat = 0;
      this._lastBlockEnergy = energyLevel;
      this._emit('energy-change', { level: energyLevel });
    }

    // --- On beat: execute move from sequence ---
    if (isBeat) {
      this._beatCount++;
      this._blockBeat = this._beatCount % 8;

      // Every 8 beats: rotate to next sequence (like duo's block transitions)
      if (this._blockBeat === 0) {
        this._currentSequence = selectSequence(energyLevel);
      }

      const move = getMoveAtBeat(this._currentSequence, this._blockBeat);
      this._executeMove(move, analysis, timestamp);
      this._emit('beat', { beat: this._beatCount, move, bpm, energy: energyLevel });
    }

    // --- Continuous: ambient LED color from spectrum ---
    if (this.ledEnabled) {
      this._sendAmbientLed(bass, mid, high, energy, isBeat, timestamp);
    }

    // --- Emit tick for visualizer ---
    this._emit('tick', { bass, mid, high, energy, isBeat, bpm, timestamp });
  }

  // Execute a single move from the sequence
  _executeMove(move, analysis, timestamp) {
    // Servo: absolute joint positions via /servo/move
    if (this.servoEnabled && move.positions) {
      const moveKey = JSON.stringify(move.positions);
      if (this.safety.shouldSend('servo', timestamp, moveKey)) {
        this.robot.servoMove(move.positions, move.duration).catch(() => {});
        this.safety.markSent('servo', timestamp, moveKey);
        const yaw = move.positions['base_yaw.pos'];
        const pitch = move.positions['base_pitch.pos'];
        this._emit('command', { type: 'servo', yaw, pitch, duration: move.duration });
      }
    }

    // Emotion (sparse — only some beats have emotion triggers)
    if (this.emotionEnabled && move.emotion) {
      if (this.safety.shouldSend('emotion', timestamp, move.emotion)) {
        const intensity = Math.min(analysis.energy / 180, 1.0);
        this.robot.emotion(move.emotion, intensity).catch(() => {});
        this.safety.markSent('emotion', timestamp, move.emotion);
        this._emit('command', { type: 'emotion', emotion: move.emotion, intensity });
      }
    }
  }

  // Continuous LED: spectrum color between beats, flash on beats
  _sendAmbientLed(bass, mid, high, energy, isBeat, timestamp) {
    let color;
    if (isBeat && energy > 80) {
      // Beat flash: bright color from spectrum + boost
      const base = spectrumColor(bass, mid, high);
      color = flashColor(base, Math.min(energy / 200, 1));
    } else {
      // Between beats: dim spectrum color
      const base = spectrumColor(bass, mid, high);
      color = dimColor(base, 0.3 + (energy / 255) * 0.3);
    }

    if (this.safety.shouldSend('led', timestamp, color)) {
      this.robot.ledSolid(color, true).catch(() => {});
      this.safety.markSent('led', timestamp, color);
    }
  }

  _emit(type, detail = {}) {
    this.dispatchEvent(new CustomEvent(type, { detail }));
  }
}
