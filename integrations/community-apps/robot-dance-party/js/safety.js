// safety.js — Rate limiter + command safety for HTTP robot control
//
// Equivalent to duo's safety_mixer.js 5-stage pipeline, adapted for
// HTTP-based control. Duo enforces joint limits + collision detection +
// LERP smoothing at 50Hz over WebSocket. We enforce rate limits and
// command deduplication over HTTP since each request has overhead.
//
// Pipeline: intent → throttle → dedup → send

export class SafetyThrottle {
  constructor() {
    // Minimum intervals between commands (ms)
    // Tuned for HTTP overhead — duo sends at 50Hz over WebSocket,
    // we limit to sustainable HTTP rates
    this.limits = {
      led:     80,   // ~12/s max (LED updates are lightweight)
      servo:   250,  // ~4/s max (servo needs time to reach position)
      emotion: 3000, // 1 per 3s (emotions are multi-second animations)
    };

    // Last send timestamps
    this._lastSend = {
      led: 0,
      servo: 0,
      emotion: 0,
    };

    // Last sent values (for dedup)
    this._lastValue = {
      led: null,
      servo: null,
      emotion: null,
    };

    // Stats
    this.stats = {
      sent: { led: 0, servo: 0, emotion: 0 },
      dropped: { led: 0, servo: 0, emotion: 0 },
    };
  }

  // Update rate limits from UI sliders
  setRate(channel, hz) {
    this.limits[channel] = Math.floor(1000 / Math.max(hz, 1));
  }

  // Returns true if the command should be sent (not throttled)
  canSend(channel, timestamp) {
    const elapsed = timestamp - (this._lastSend[channel] || 0);
    return elapsed >= (this.limits[channel] || 0);
  }

  // Mark a command as sent
  markSent(channel, timestamp, value = null) {
    this._lastSend[channel] = timestamp;
    this._lastValue[channel] = value;
    this.stats.sent[channel]++;
  }

  // Mark a command as dropped (throttled)
  markDropped(channel) {
    this.stats.dropped[channel]++;
  }

  // Check if value is same as last sent (dedup)
  isDuplicate(channel, value) {
    const last = this._lastValue[channel];
    if (!last || !value) return false;
    if (typeof value === 'string') return value === last;
    if (Array.isArray(value) && Array.isArray(last)) {
      // For LED colors: skip if delta is tiny (avoid flickering)
      if (value.length === 3 && last.length === 3) {
        const delta = Math.abs(value[0] - last[0])
          + Math.abs(value[1] - last[1])
          + Math.abs(value[2] - last[2]);
        return delta < 15; // Skip if RGB difference < 15 total
      }
    }
    return JSON.stringify(value) === JSON.stringify(last);
  }

  // Convenience: check throttle + dedup, return whether to send
  shouldSend(channel, timestamp, value = null) {
    if (!this.canSend(channel, timestamp)) {
      this.markDropped(channel);
      return false;
    }
    if (value && this.isDuplicate(channel, value)) {
      this.markDropped(channel);
      return false;
    }
    return true;
  }

  // Reset stats
  resetStats() {
    for (const ch of Object.keys(this.stats.sent)) {
      this.stats.sent[ch] = 0;
      this.stats.dropped[ch] = 0;
    }
  }
}
