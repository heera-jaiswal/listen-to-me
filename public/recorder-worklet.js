class RecorderProcessor extends AudioWorkletProcessor {
  constructor(options) {
    super();
    const target = options.processorOptions?.targetRate || 16000;
    this.step = sampleRate / target;
    this.t = 0;
    this.prev = 0;
    this.frame = Math.round(target / 10);
    this.buf = new Int16Array(this.frame);
    this.n = 0;
    this.sq = 0;
    this.cnt = 0;
  }

  push(v) {
    const s = Math.max(-1, Math.min(1, v));
    this.buf[this.n++] = s < 0 ? s * 0x8000 : s * 0x7fff;
    if (this.n === this.frame) {
      const rms = Math.sqrt(this.sq / Math.max(1, this.cnt));
      this.port.postMessage({ pcm: this.buf.buffer, rms }, [this.buf.buffer]);
      this.buf = new Int16Array(this.frame);
      this.n = 0;
      this.sq = 0;
      this.cnt = 0;
    }
  }

  process(inputs) {
    const ch = inputs[0] && inputs[0][0];
    if (!ch) return true;
    for (let i = 0; i < ch.length; i++) this.sq += ch[i] * ch[i];
    this.cnt += ch.length;

    if (this.step === 1) {
      for (let i = 0; i < ch.length; i++) this.push(ch[i]);
      return true;
    }

    let t = this.t;
    while (t < ch.length - 1) {
      const i = Math.floor(t);
      const f = t - i;
      const a = i < 0 ? this.prev : ch[i];
      const b = ch[i + 1];
      this.push(a + (b - a) * f);
      t += this.step;
    }
    this.t = t - ch.length;
    this.prev = ch[ch.length - 1];
    return true;
  }
}

registerProcessor('recorder', RecorderProcessor);
