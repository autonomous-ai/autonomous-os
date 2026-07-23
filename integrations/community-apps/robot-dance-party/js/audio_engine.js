// audio_engine.js — Web Audio API analysis + BPM detection
//
// FFT analysis (bass/mid/high/energy) + energy-based beat detection run
// inline per frame. BPM estimation uses realtime-bpm-analyzer (CDN) when
// available, falls back to median-interval estimation if CDN is unreachable.

// Lazy-loaded from CDN on first use
let _bpmLib = null;
async function loadBpmLib() {
  if (_bpmLib) return _bpmLib;
  try {
    _bpmLib = await import('https://cdn.jsdelivr.net/npm/realtime-bpm-analyzer/+esm');
  } catch (_) {
    _bpmLib = null; // CDN unreachable — fall back to manual BPM
  }
  return _bpmLib;
}

export class AudioEngine extends EventTarget {
  constructor() {
    super();
    this.ctx = null;
    this.analyser = null;
    this.source = null;
    this.audioEl = null;

    // BPM analyzer (realtime-bpm-analyzer AudioWorklet, if available)
    this._bpmAnalyzer = null;
    this._lowpass = null;

    // FFT data buffers
    this.freqData = null;
    this.timeData = null;

    // Beat detection state (energy-based onset)
    this._prevEnergy = 0;
    this._energyHistory = new Float32Array(30); // ~0.5s window — shorter = average stays lower = more sensitive
    this._historyIdx = 0;
    this._lastBeatTime = 0;

    // Fallback BPM estimation (used when realtime-bpm-analyzer unavailable)
    this._beatIntervals = [];

    // BPM value (updated by analyzer or fallback)
    this.bpm = 0;

    // Beat sensitivity: fraction above rolling average to trigger beat
    // Slider maps [20..90] → [0.20..0.02], default 55 → 0.08
    this.sensitivity = 0.08;

    // Band energies (exposed for dance engine)
    this.bass = 0;
    this.mid = 0;
    this.high = 0;
    this.energy = 0;

    this.isPlaying = false;
    this.isMicMode = false;
  }

  async _initContext() {
    if (this.ctx) {
      if (this.ctx.state === 'suspended') this.ctx.resume();
      return;
    }
    this.ctx = new (window.AudioContext || window.webkitAudioContext)();
    this.ctx.resume();
    this.analyser = this.ctx.createAnalyser();
    this.analyser.fftSize = 1024;
    this.analyser.smoothingTimeConstant = 0.3; // Low = sharp transients, aggressive beat detection
    this.freqData = new Uint8Array(this.analyser.frequencyBinCount);
    this.timeData = new Uint8Array(this.analyser.fftSize);

    // Try loading realtime-bpm-analyzer from CDN
    const lib = await loadBpmLib();
    if (lib) {
      try {
        this._bpmAnalyzer = await lib.createRealtimeBpmAnalyzer(this.ctx, {
          continuousAnalysis: true,
        });
        this._lowpass = lib.getBiquadFilter(this.ctx);

        this._bpmAnalyzer.on('bpm', ({ bpm }) => {
          if (bpm && bpm.length > 0) {
            this.bpm = Math.round(bpm[0].tempo);
          }
        });
      } catch (_) {
        this._bpmAnalyzer = null;
        this._lowpass = null;
      }
    }
  }

  // Wire source through both the FFT analyser and the BPM analyzer (if available)
  _connectSource(source, toDestination) {
    // FFT analysis chain
    source.connect(this.analyser);
    if (toDestination) {
      this.analyser.connect(this.ctx.destination);
    }

    // BPM analyzer chain (parallel path through lowpass filter)
    if (this._bpmAnalyzer && this._lowpass) {
      source.connect(this._lowpass).connect(this._bpmAnalyzer.node);
    }
  }

  // Load audio file and connect to analyser
  async loadFile(file) {
    this.stop();
    await this._initContext();
    this.isMicMode = false;

    // Must create a fresh <audio> element each time —
    // createMediaElementSource can only bind once per element.
    this.audioEl = document.createElement('audio');
    this.audioEl.addEventListener('ended', () => {
      this.isPlaying = false;
      this._emit('ended');
    });

    this.audioEl.src = URL.createObjectURL(file);
    this.source = this.ctx.createMediaElementSource(this.audioEl);
    this._connectSource(this.source, true);

    this._resetBpmAnalyzer();
    this.audioEl.play();
    this.isPlaying = true;
    this._resetBeatState();
    this._emit('playing', { name: file.name, duration: 0 });

    this.audioEl.addEventListener('loadedmetadata', () => {
      this._emit('duration', { duration: this.audioEl.duration });
    }, { once: true });
  }

  // Load audio from URL (downloaded YouTube audio served by start_server.py)
  async loadUrl(url, title) {
    this.stop();
    await this._initContext();
    this.isMicMode = false;

    this.audioEl = document.createElement('audio');
    this.audioEl.crossOrigin = 'anonymous';
    this.audioEl.addEventListener('ended', () => {
      this.isPlaying = false;
      this._emit('ended');
    });

    this.audioEl.src = url;
    this.source = this.ctx.createMediaElementSource(this.audioEl);
    this._connectSource(this.source, true);

    this._resetBpmAnalyzer();
    this.audioEl.play();
    this.isPlaying = true;
    this._resetBeatState();
    this._emit('playing', { name: title || 'YouTube', duration: 0 });

    this.audioEl.addEventListener('loadedmetadata', () => {
      this._emit('duration', { duration: this.audioEl.duration });
    }, { once: true });
  }

  // Connect microphone
  async loadMic() {
    this.stop();
    await this._initContext();
    this.isMicMode = true;

    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });

    this.source = this.ctx.createMediaStreamSource(stream);

    // Boost mic gain
    const gain = this.ctx.createGain();
    gain.gain.value = 2.0;
    this.source.connect(gain);
    gain.connect(this.analyser);
    // Don't connect to destination (feedback prevention)

    // BPM analyzer (parallel path from gain node)
    if (this._bpmAnalyzer && this._lowpass) {
      gain.connect(this._lowpass).connect(this._bpmAnalyzer.node);
    }

    this._resetBpmAnalyzer();
    this.isPlaying = true;
    this._resetBeatState();
    this._emit('playing', { name: 'Microphone', duration: Infinity });
  }

  // Capture system/tab audio via getDisplayMedia (Chrome)
  async loadTabAudio() {
    this.stop();
    await this._initContext();
    this.isMicMode = true;

    const stream = await navigator.mediaDevices.getDisplayMedia({
      video: true,
      audio: true,
    });
    stream.getVideoTracks().forEach(t => t.stop());

    if (!stream.getAudioTracks().length) {
      throw new Error('No audio track — make sure you checked "Share tab audio"');
    }

    this.source = this.ctx.createMediaStreamSource(stream);
    this._connectSource(this.source, false);

    this._resetBpmAnalyzer();
    this.isPlaying = true;
    this._resetBeatState();
    this._emit('playing', { name: 'Tab Audio', duration: Infinity });

    stream.getAudioTracks()[0].addEventListener('ended', () => {
      this.isPlaying = false;
      this._emit('ended');
    });
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

    // Weighted total (bass-heavy)
    this.energy = (this.bass * 2.5 + this.mid * 1.0 + this.high * 0.5) / 4;

    // --- Beat detection ---
    // Rolling average as baseline
    this._energyHistory[this._historyIdx % this._energyHistory.length] = this.energy;
    this._historyIdx++;
    let avgEnergy = 0;
    const filled = Math.min(this._historyIdx, this._energyHistory.length);
    for (let i = 0; i < filled; i++) avgEnergy += this._energyHistory[i];
    avgEnergy /= filled;

    // Beat = current energy exceeds rolling average by sensitivity fraction + cooldown
    const minInterval = 200; // ~300 BPM max
    const isBeat = this.energy > avgEnergy * (1 + this.sensitivity)
      && this.energy > 15
      && (timestamp - this._lastBeatTime) > minInterval;

    if (isBeat) {
      this._lastBeatTime = timestamp;

      // Fallback BPM: median-interval estimation when analyzer unavailable
      if (!this._bpmAnalyzer && this._lastBeatTime > 0) {
        const prev = this._beatIntervals.length > 0
          ? timestamp - this._beatIntervals._lastTs : 0;
        if (!this._beatIntervals._lastTs) {
          this._beatIntervals._lastTs = timestamp;
        } else {
          const interval = timestamp - this._beatIntervals._lastTs;
          this._beatIntervals._lastTs = timestamp;
          if (interval > 250 && interval < 1500) { // 40-240 BPM
            this._beatIntervals.push(interval);
            if (this._beatIntervals.length > 16) this._beatIntervals.shift();
            const sorted = [...this._beatIntervals].sort((a, b) => a - b);
            const median = sorted[Math.floor(sorted.length / 2)];
            this.bpm = Math.round(60000 / median);
          }
        }
      }
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
    this._beatIntervals._lastTs = 0;
    this.bpm = 0;
    this.bass = this.mid = this.high = this.energy = 0;
  }

  _resetBpmAnalyzer() {
    if (this._bpmAnalyzer) {
      try { this._bpmAnalyzer.reset(); } catch (_) {}
    }
  }

  _emit(type, detail = {}) {
    this.dispatchEvent(new CustomEvent(type, { detail }));
  }
}
