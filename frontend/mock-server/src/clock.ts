/**
 * Controllable server clock for time travel. Mock "now" advances with real time
 * times `speed` from wherever it was last set, so the dev panel can jump to any
 * moment and (optionally) let it run faster, or freeze it (speed 0).
 */
export class MockClock {
  private baseReal = Date.now();
  private baseMock: number;
  private _speed = 1;

  constructor(initialIso: string) {
    this.baseMock = Date.parse(initialIso);
  }

  now(): number {
    return this.baseMock + (Date.now() - this.baseReal) * this._speed;
  }

  iso(): string {
    return new Date(this.now()).toISOString();
  }

  get speed(): number {
    return this._speed;
  }

  set(ms: number): void {
    this.baseMock = ms;
    this.baseReal = Date.now();
  }

  setIso(iso: string): void {
    const ms = Date.parse(iso);
    if (!Number.isFinite(ms)) throw new Error(`bad ISO time: ${iso}`);
    this.set(ms);
  }

  advance(ms: number): void {
    this.set(this.now() + ms);
  }

  setSpeed(speed: number): void {
    const n = this.now();
    this._speed = speed;
    this.set(n);
  }
}
