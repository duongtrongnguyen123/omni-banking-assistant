import type { Lang } from "./i18n/strings";

export const formatVND = (n: number, lang: Lang = "vi"): string => {
  if (lang === "en") {
    // EN uses comma-thousands and the ISO ₫ glyph spaced after the number,
    // matching how Vietnamese-language English finance writing usually does it.
    return n.toLocaleString("en-US") + " ₫";
  }
  return n.toLocaleString("vi-VN") + "đ";
};

export const formatDate = (iso: string): string => {
  const d = new Date(iso);
  const pad = (x: number) => x.toString().padStart(2, "0");
  return `${pad(d.getDate())}/${pad(d.getMonth() + 1)}/${d.getFullYear()}`;
};

export const formatDateTime = (iso: string): string => {
  const d = new Date(iso);
  const pad = (x: number) => x.toString().padStart(2, "0");
  return `${pad(d.getDate())}/${pad(d.getMonth() + 1)} · ${pad(d.getHours())}:${pad(
    d.getMinutes(),
  )}`;
};
