/**
 * PlaybackController - Persistent audio element for mobile-compatible playback.
 * Handles AudioContext unlocking across browsers/platforms and Raw PCM streaming.
 */

/** Ensure a URL is absolute (relative paths get prefixed with origin). */
function ensureAbsoluteUrl(url) {
  try {
    new URL(url);
    return url;
  } catch {
    if (!url) return url;
    if (url.startsWith('/')) return `${location.origin}${url}`;
    return `${location.origin}/${url}`;
  }
}

export class PlaybackController {
  constructor(audioMgr) {
    this.audioMgr = audioMgr;
    this._inited = false;
    this._unlocked = false;
    this.el = null;
    this._analyserAttached = false;
    this._finishPlaybackWait = null;
    this._playbackGeneration = 0;
    this._currentRawSource = null;
    this._currentAbortController = null;
    this.nextStartTime = 0;
  }

  initPersistent() {
    if (this._inited) return;
    this._inited = true;

    if (!this.audioMgr.audioElement) {
      const a = document.createElement('audio');
      a.crossOrigin = 'anonymous';
      a.preload = 'auto';
      a.playsInline = true;
      a.setAttribute('playsinline', '');
      a.setAttribute('webkit-playsinline', '');
      a.style.display = 'none';
      document.body.appendChild(a);
      this.audioMgr.audioElement = a;
    }
    this.el = this.audioMgr.audioElement;

    try {
      if (!this.audioMgr.audioContext) {
        const AC = window.AudioContext || window.webkitAudioContext;
        if (AC) {
          this.audioMgr.audioContext = new AC();
        }
      }

      if (this.audioMgr.audioContext && !this.audioMgr.analyser) {
        this._tryAttachAnalyser();
      }

      document.addEventListener('visibilitychange', async () => {
        if (document.visibilityState === 'visible' &&
            this.audioMgr.audioContext &&
            this.audioMgr.audioContext.state === 'suspended') {
          try {
            await this.audioMgr.audioContext.resume();
          } catch (e) {}
        }
      });
    } catch (e) {
      console.warn('PlaybackController init error:', e);
    }
  }

  async unlockOnce() {
    if (this._unlocked) return true;
    this.initPersistent();

    let ctx = null;
    try {
      if (!this.audioMgr.audioContext) {
        const AC = window.AudioContext || window.webkitAudioContext;
        if (AC) {
          this.audioMgr.audioContext = new AC();
        }
      }
      ctx = this.audioMgr.audioContext;
      if (ctx && ctx.state === 'suspended') {
        try { await ctx.resume(); } catch (e) { console.warn('resume() failed:', e); }
      }
    } catch (e) {
      console.warn('AudioContext creation/resume failed:', e);
    }

    try {
      if (ctx && ctx.state === 'running') {
        const sampleRate = ctx.sampleRate || 44100;
        const length = Math.max(1, Math.floor(sampleRate * 0.01));
        const buffer = ctx.createBuffer(1, length, sampleRate);
        const src = ctx.createBufferSource();
        src.buffer = buffer;
        src.connect(ctx.destination);
        src.start(0);
        await new Promise(res => setTimeout(res, 40));
        try { src.stop(); } catch (e) {}
        this._unlocked = true;
        this._tryAttachAnalyser();
        return true;
      }
    } catch (e) {
      console.warn('Silent buffer unlock failed:', e);
    }

    try {
      const el = this.el;
      if (!el) throw new Error('No audio element');
      if (!el.src) {
        el.src = 'data:audio/wav;base64,UklGRiQAAABXQVZFZm10IBAAAAABAAEAESsAACJWAAACABAAZGF0YQAAAAA=';
      }
      el.muted = true;
      el.playsInline = true;
      el.setAttribute('playsinline', '');
      el.setAttribute('webkit-playsinline', '');

      const p = new Promise((resolve, reject) => {
        let done = false;
        const onPlaying = () => { if (!done) { done = true; cleanup(); resolve(true); } };
        const onError = () => { if (!done) { done = true; cleanup(); reject(new Error('audio error')); } };
        const timeoutId = setTimeout(() => { if (!done) { done = true; cleanup(); reject(new Error('timeout')); } }, 1200);
        function cleanup() {
          el.removeEventListener('playing', onPlaying);
          el.removeEventListener('error', onError);
          clearTimeout(timeoutId);
        }
        el.addEventListener('playing', onPlaying);
        el.addEventListener('error', onError);
        try {
          const prom = el.play();
          if (prom && prom.catch) prom.catch(() => {});
        } catch (err) { cleanup(); reject(err); }
      });

      await p;
      try { el.pause(); el.currentTime = 0; } catch (e) {}
      el.muted = false;
      this._unlocked = true;
      this._tryAttachAnalyser();
      return true;
    } catch (e) {
      console.warn('Muted element fallback failed:', e);
    }

    return false;
  }

  _tryAttachAnalyser() {
    try {
      if (this.audioMgr.audioContext && this.el && !this.audioMgr.analyser && !this._analyserAttached) {
        try {
          const src = this.audioMgr.audioContext.createMediaElementSource(this.el);
          const analyser = this.audioMgr.audioContext.createAnalyser();
          analyser.fftSize = 2048;
          src.connect(analyser);
          analyser.connect(this.audioMgr.audioContext.destination);
          this.audioMgr.analyser = analyser;
          this.audioMgr.timeDomainData = new Uint8Array(analyser.fftSize);
          this.audioMgr.freqData = new Uint8Array(analyser.frequencyBinCount);
          this._analyserAttached = true;
        } catch (e) {
          console.warn('attachAnalyser failed:', e);
        }
      }
    } catch (e) {
      console.warn('Error in _tryAttachAnalyser:', e);
    }
  }

  async playAudioUrl(url) {
    if (!url) return false;
    const abs = ensureAbsoluteUrl(url);
    this.initPersistent();

    if (!this._unlocked) {
      try { await this.unlockOnce(); } catch (e) {}
    }

    this.stop();

    try {
      const el = this.el;
      if (!el) throw new Error('No audio element');

      console.debug(`[Audio] Playing via HTML5 element: ${abs}`);
      el.src = abs;
      el.volume = 1.0; // Force max volume
      el.load();

      // Attempt to play and handle the promise
      try {
        await el.play();
      } catch (err) {
        // Catch AbortError specifically: happens if stop() is called
        // or src is changed before the browser starts playback.
        if (err.name === 'AbortError') {
          console.warn('[Audio] play() was aborted - this is expected during rapid changes');
          return false;
        }
        throw err; // rethrow other errors
      }
      console.debug('[Audio] Playback started successfully');

      window.dispatchEvent(new CustomEvent('audio-started', {
        detail: { url: abs, startTime: 0 }
      }));

      return true;
    } catch (err) {
      console.error('HTML5 audio playback failed, attempting emergency AudioContext fallback:', err);

      // EMERGENCY FALLBACK: If <audio> fails, try to fetch and decode manually one last time
      try {
        const response = await fetch(abs);
        const arrayBuffer = await response.arrayBuffer();
        const ctx = this.audioMgr.audioContext;
        const audioBuffer = await ctx.decodeAudioData(arrayBuffer);
        const source = ctx.createBufferSource();
        source.buffer = audioBuffer;
        source.connect(ctx.destination);
        source.start(0);
        this._currentRawSource = source;
        console.debug('[Audio] Emergency fallback successful');
        return true;
      } catch (fallbackErr) {
        console.error('Emergency fallback also failed:', fallbackErr);
        return false;
      }
    }
  }

  stop() {
    this._playbackGeneration += 1;
    if (this._finishPlaybackWait) this._finishPlaybackWait();

    try {
      if (this.el) {
        this.el.pause();
        this.el.currentTime = 0;
        this.el.src = "";
        this.el.load();
      }
    } catch (e) {
      console.warn('[playback] failed to stop active audio:', e);
    }
  }

  waitUntilEnded(expectedSeconds = 0) {
    const el = this.el;
    if (!el || el.ended) return Promise.resolve();

    return new Promise((resolve) => {
      let done = false;
      const finish = (why) => {
        if (done) return;
        done = true;
        el.removeEventListener('ended', onEnded);
        el.removeEventListener('error', onError);
        clearTimeout(timer);
        if (this._finishPlaybackWait === finish) this._finishPlaybackSWait = null;
        if (why !== 'ended') console.warn(`[playback] clip finished via ${why}`);
        resolve();
      };
      const onEnded = () => finish('ended');
      const onError = () => finish('error');

      const known = Number.isFinite(el.duration) && el.duration > 0
        ? el.duration
        : expectedSeconds;
      const remaining = Math.max(0, known - (el.currentTime || 0));
      const timer = setTimeout(() => finish('timeout'), (remaining + 5) * 1000);
      this._finishPlaybackWait = finish;

      el.addEventListener('ended', onEnded);
      el.addEventListener('error', onError);
    });
  }
}
