// audio_engine.js — Web Audio API analysis + real-time beat detection
//
// Similar role to duo's live_groove.js real-time pipeline:
//   source → gain → analyser → beat detection → BPM tracking
//
// Unlike duo's beat_bandit (pre-analyzed JSON), we do everything in
// real-time using energy-based onset detection.

export class AudioEngine extends EventTarget {
  constructor() {
    super();
    this.ctx = null;
    this.analyser = null;
    this.source = null;
    this.audioEl = null;

    // FFT data buffers
    this.freqData = null;
    this.timeData = null;

    // Beat detection state (energy-based, like live_groove)
    this._prevEnergy = 0;
    this._energyHistory = new Float32Array(43); // ~1s at 43fps
    this._historyIdx = 0;
    this._lastBeatTime = 0;

    // BPM estimation (like duo's BpmStabilityTracker)
    this._beatIntervals = [];
    this.bpm = 0;
    this._bpmLocked = false;

    // Band energies (exposed for dance engine)
    this.bass = 0;
    this.mid = 0;
    this.high = 0;
    this.energy = 0;

    this.isPlaying = false;
    this.isMicMode = false;
  }

  _initContext() {
    if (this.ctx) return;
    this.ctx = new (window.AudioContext || window.webkitAudioContext)();
    this.analyser = this.ctx.createAnalyser();
    this.analyser.fftSize = 1024;
    this.analyser.smoothingTimeConstant = 0.75;
    this.freqData = new Uint8Array(this.analyser.frequencyBinCount);
    this.timeData = new Uint8Array(this.analyser.fftSize);
  }

  // Load audio file and connect to analyser
  loadFile(file) {
    this.stop();
    this._initContext();
    this.isMicMode = false;

    if (!this.audioEl) {
      this.audioEl = document.createElement('audio');
      this.audioEl.addEventListener('ended', () => {
        this.isPlaying = false;
        this._emit('ended');
      });
    }

    const url = URL.createObjectURL(file);
    this.audioEl.src = url;

    // Disconnect old source
    if (this.source) { try { this.source.disconnect(); } catch (_) {} }
    this.source = this.ctx.createMediaElementSource(this.audioEl);
    this.source.connect(this.analyser);
    this.analyser.connect(this.ctx.destination);

    this.audioEl.play();
    this.isPlaying = true;
    this._resetBeatState();
    this._emit('playing', { name: file.name, duration: 0 });

    // Duration becomes available async
    this.audioEl.addEventListener('loadedmetadata', () => {
      this._emit('duration', { duration: this.audioEl.duration });
    }, { once: true });
  }

  // Connect microphone (like duo's live_groove WebRTC audio source)
  async loadMic() {
    this.stop();
    this._initContext();
    this.isMicMode = true;

    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });

    if (this.source) { try { this.source.disconnect(); } catch (_) {} }
    this.source = this.ctx.createMediaStreamSource(stream);

    // Boost mic gain (duo uses 20x for Opus-attenuated WebRTC audio)
    const gain = this.ctx.createGain();
    gain.gain.value = 2.0;
    this.source.connect(gain);
    gain.connect(this.analyser);
    // Don't connect to destination (feedback prevention)

    this.isPlaying = true;
    this._resetBeatState();
    this._emit('playing', { name: 'Microphone', duration: Infinity });
  }

  // Transport controls
  pause() {
    if (this.isMicMode) return;
    if (this.audioEl) this.audioEl.pause();
    this.isPlaying = false;
  }

  resume() {
    if (this.isMicMode) return;
    if (this.audioEl) this.audioEl.play();
    this.isPlaying = true;
  }

  stop() {
    this.isPlaying = false;
    if (this.isMicMode && this.source?.mediaStream) {
      this.source.mediaStream.getTracks().forEach(t => t.stop());
    }
    if (this.audioEl) {
      this.audioEl.pause();
      this.audioEl.currentTime = 0;
    }
    this.isMicMode = false;
    this._resetBeatState();
  }

  get currentTime() {
    return this.audioEl?.currentTime || 0;
  }

  get duration() {
    return this.audioEl?.duration || 0;
  }

  // --- Per-frame analysis (called from dance loop) ---

  // Returns { bass, mid, high, energy, isBeat, bpm }
  analyze(timestamp) {
    if (!this.analyser || !this.isPlaying) {
      return { bass: 0, mid: 0, high: 0, energy: 0, isBeat: false, bpm: 0 };
    }

    this.analyser.getByteFrequencyData(this.freqData);
    this.analyser.getByteTimeDomainData(this.timeData);

    // --- Frequency band energies ---
    const bins = this.freqData.length;
    const bassEnd = Math.floor(bins * 0.06);    // ~0-250 Hz
    const midEnd = Math.floor(bins * 0.35);     // ~250-3.5kHz
    const highEnd = bins;

    let bassSum = 0, midSum = 0, highSum = 0;
    for (let i = 0; i < bassEnd; i++) bassSum += this.freqData[i];
    for (let i = bassEnd; i < midEnd; i++) midSum += this.freqData[i];
    for (let i = midEnd; i < highEnd; i++) highSum += this.freqData[i];

    this.bass = bassSum / (bassEnd || 1);
    this.mid = midSum / ((midEnd - bassEnd) || 1);
    this.high = highSum / ((highEnd - midEnd) || 1);

    // Weighted total (bass-heavy, like duo)
    this.energy = (this.bass * 2.5 + this.mid * 1.0 + this.high * 0.5) / 4;

    // --- Beat detection ---
    // Energy-based onset detection with adaptive threshold
    // Similar to duo's volume-gated approach
    const energyDelta = this.energy - this._prevEnergy;

    // Rolling average for adaptive threshold
    this._energyHistory[this._historyIdx % this._energyHistory.length] = this.energy;
    this._historyIdx++;
    let avgEnergy = 0;
    const filled = Math.min(this._historyIdx, this._energyHistory.length);
    for (let i = 0; i < filled; i++) avgEnergy += this._energyHistory[i];
    avgEnergy /= filled;

    // Beat = energy spike above rolling average + minimum energy + cooldown
    const minInterval = 220; // ~270 BPM max (prevents double-triggers)
    const isBeat = energyDelta > (avgEnergy * 0.45)
      && this.energy > 50
      && (timestamp - this._lastBeatTime) > minInterval;

    // Smooth energy tracking (EMA like duo's asymmetric approach)
    this._prevEnergy = this.energy * 0.6 + this._prevEnergy * 0.4;

    // --- BPM estimation ---
    if (isBeat) {
      if (this._lastBeatTime > 0) {
        const interval = timestamp - this._lastBeatTime;
        if (interval > 250 && interval < 1500) { // 40-240 BPM range
          this._beatIntervals.push(interval);
          if (this._beatIntervals.length > 16) this._beatIntervals.shift();

          // Median filter (more robust than mean for BPM)
          const sorted = [...this._beatIntervals].sort((a, b) => a - b);
          const median = sorted[Math.floor(sorted.length / 2)];
          this.bpm = Math.round(60000 / median);
        }
      }
      this._lastBeatTime = timestamp;
    }

    return {
      bass: this.bass,
      mid: this.mid,
      high: this.high,
      energy: this.energy,
      isBeat,
      bpm: this.bpm,
    };
  }

  _resetBeatState() {
    this._prevEnergy = 0;
    this._energyHistory.fill(0);
    this._historyIdx = 0;
    this._lastBeatTime = 0;
    this._beatIntervals = [];
    this.bpm = 0;
    this.bass = this.mid = this.high = this.energy = 0;
  }

  _emit(type, detail = {}) {
    this.dispatchEvent(new CustomEvent(type, { detail }));
  }
}
