const mojibakePattern = /(Ã|Â|Æ|Ä|áº|á»|â€|â€”|â€¦)/;

function decodeMojibake(text: string): string {
  if (!mojibakePattern.test(text)) return text;
  try {
    const bytes = Uint8Array.from(Array.from(text), (ch) => ch.charCodeAt(0) & 0xff);
    const decoded = new TextDecoder("utf-8", { fatal: false }).decode(bytes);
    const beforeBad = (text.match(/\uFFFD|\?/g) ?? []).length;
    const afterBad = (decoded.match(/\uFFFD|\?/g) ?? []).length;
    return afterBad <= beforeBad + 1 ? decoded : text;
  } catch {
    return text;
  }
}

export function repairVietnameseText(input: string | null | undefined): string {
  if (!input) return "";
  let text = decodeMojibake(input);
  const replacements: Array<[RegExp, string]> = [
    [/\bchuy\?n\b/gi, "chuyển"],
    [/\btri\?u\b/gi, "triệu"],
    [/\bti\?n\b/gi, "tiền"],
    [/\bng\?\?i\b/gi, "người"],
    [/\bnh\?n\b/gi, "nhận"],
    [/\bmu\?n\b/gi, "muốn"],
    [/\bb\?n\b/gi, "bạn"],
    [/\bm\?nh\b/gi, "mình"],
    [/\br\?\b/gi, "rõ"],
    [/\bc\? th\?\b/gi, "cụ thể"],
    [/\bh\?n\b/gi, "hơn"],
    [/\bv\? d\?\b/gi, "ví dụ"],
    [/\bcho m\?\b/gi, "cho mẹ"],
    [/\bm\?$/gi, "mẹ"],
  ];
  for (const [pattern, replacement] of replacements) {
    text = text.replace(pattern, replacement);
  }
  return text;
}
