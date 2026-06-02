export const formatVND = (n: number): string =>
  n.toLocaleString("vi-VN") + "đ";

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
