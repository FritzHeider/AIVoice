/** OKLCH → sRGB conversion and WCAG 2.x contrast (https://bottosson.github.io/posts/oklab/, WCAG 2.2 §1.4.3). */

export interface Oklch {
  l: number;
  c: number;
  h: number;
}

const OKLCH_RE = /^oklch\(\s*([\d.]+)%\s+([\d.]+)\s+([\d.]+)\s*\)$/;

export function parseOklch(value: string): Oklch {
  const match = OKLCH_RE.exec(value.trim());
  if (match === null) {
    throw new Error(`not an oklch() colour: ${value}`);
  }
  return { l: Number(match[1]) / 100, c: Number(match[2]), h: Number(match[3]) };
}

function clamp01(x: number): number {
  return Math.min(1, Math.max(0, x));
}

/** Linear-light sRGB channels (gamut-clipped) for an OKLCH colour. */
export function oklchToLinearSrgb({ l, c, h }: Oklch): [number, number, number] {
  const rad = (h * Math.PI) / 180;
  const a = c * Math.cos(rad);
  const b = c * Math.sin(rad);
  const l_ = (l + 0.3963377774 * a + 0.2158037573 * b) ** 3;
  const m_ = (l - 0.1055613458 * a - 0.0638541728 * b) ** 3;
  const s_ = (l - 0.0894841775 * a - 1.291485548 * b) ** 3;
  return [
    clamp01(4.0767416621 * l_ - 3.3077115913 * m_ + 0.2309699292 * s_),
    clamp01(-1.2684380046 * l_ + 2.6097574011 * m_ - 0.3413193965 * s_),
    clamp01(-0.0041960863 * l_ - 0.7034186147 * m_ + 1.707614701 * s_),
  ];
}

export function relativeLuminance(colour: Oklch): number {
  const [r, g, b] = oklchToLinearSrgb(colour);
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}

export function contrastRatio(a: Oklch, b: Oklch): number {
  const [hi, lo] = [relativeLuminance(a), relativeLuminance(b)].sort((x, y) => y - x) as [
    number,
    number,
  ];
  return (hi + 0.05) / (lo + 0.05);
}
