// Browser Web Speech API wrapper for Vietnamese TTS.
//
// Picks the first `vi-VN` voice surfaced by `speechSynthesis.getVoices()`
// and falls back to the platform default. Voices on Chrome are loaded
// asynchronously, so we keep a tiny voice cache that's primed on the
// `voiceschanged` event.

let cachedVoice: SpeechSynthesisVoice | null | undefined;

const supported = (): boolean =>
  typeof window !== "undefined" && "speechSynthesis" in window;

const pickVietnameseVoice = (): SpeechSynthesisVoice | null => {
  if (!supported()) return null;
  const voices = window.speechSynthesis.getVoices();
  if (!voices || voices.length === 0) return null;
  const vi = voices.find((v) => v.lang === "vi-VN" || v.lang?.toLowerCase().startsWith("vi"));
  return vi ?? null;
};

const primeVoices = () => {
  if (!supported()) return;
  // Trigger the async voice list load (Chrome).
  window.speechSynthesis.getVoices();
  window.speechSynthesis.addEventListener?.("voiceschanged", () => {
    cachedVoice = pickVietnameseVoice();
  });
};

if (supported()) primeVoices();

const stripMarkdown = (s: string): string =>
  s
    // Code fences and inline code.
    .replace(/```[\s\S]*?```/g, " ")
    .replace(/`([^`]+)`/g, "$1")
    // Bold / italic / underscore emphasis.
    .replace(/\*\*([^*]+)\*\*/g, "$1")
    .replace(/\*([^*]+)\*/g, "$1")
    .replace(/__([^_]+)__/g, "$1")
    .replace(/_([^_]+)_/g, "$1")
    // Headings, blockquotes, list bullets at line start.
    .replace(/^\s{0,3}#{1,6}\s+/gm, "")
    .replace(/^\s{0,3}>\s?/gm, "")
    .replace(/^\s*[-*+]\s+/gm, "")
    // Links: [text](url) → text
    .replace(/\[([^\]]+)\]\([^)]+\)/g, "$1")
    // Collapse whitespace.
    .replace(/\s+/g, " ")
    .trim();

/**
 * Speak the given text using a Vietnamese voice when available.
 * No-op when the platform doesn't support Web Speech, when text is empty,
 * or when a previous utterance is already playing (cancelled before next).
 */
export const speak = (text: string): void => {
  if (!supported()) return;
  const cleaned = stripMarkdown(text);
  if (!cleaned) return;
  try {
    const synth = window.speechSynthesis;
    // Cancel any in-flight utterance — keeps the demo snappy when messages
    // arrive in quick succession.
    synth.cancel();
    const utter = new SpeechSynthesisUtterance(cleaned);
    if (cachedVoice === undefined) cachedVoice = pickVietnameseVoice();
    if (cachedVoice) {
      utter.voice = cachedVoice;
      utter.lang = cachedVoice.lang;
    } else {
      utter.lang = "vi-VN";
    }
    utter.rate = 1.0;
    utter.pitch = 1.0;
    utter.volume = 1.0;
    synth.speak(utter);
  } catch {
    // Browser quirks (Safari private mode etc.) — fail silently.
  }
};

export const cancelSpeech = (): void => {
  if (!supported()) return;
  try {
    window.speechSynthesis.cancel();
  } catch {
    /* ignore */
  }
};

export const isSpeechSupported = supported;
