import type { OmniResponse, RecentRecipient } from "../types";

const HEADERS = { "Content-Type": "application/json", "x-user-id": "u_an" };

async function jsonFetch<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, {
    ...init,
    headers: { ...HEADERS, ...(init?.headers ?? {}) },
  });
  if (!res.ok) {
    let detail = "";
    try {
      detail = (await res.json()).detail ?? "";
    } catch {
      // ignore
    }
    throw new Error(`${res.status} ${detail || res.statusText}`);
  }
  return res.json();
}

export const api = {
  chat: (message: string) =>
    jsonFetch<OmniResponse>("/api/chat", {
      method: "POST",
      body: JSON.stringify({ message }),
    }),
  confirm: (
    draftId: string,
    body: {
      otp?: string;
      biometric_verified?: boolean;
      source_account_id?: string;
    },
  ) =>
    jsonFetch<OmniResponse>(`/api/transactions/${draftId}/confirm`, {
      method: "POST",
      body: JSON.stringify({
        otp: body.otp,
        biometric_verified: body.biometric_verified,
        source_account_id: body.source_account_id,
      }),
    }),
  cancel: (draftId: string) =>
    jsonFetch<OmniResponse>(`/api/transactions/${draftId}/cancel`, {
      method: "POST",
    }),
  select: (draftId: string, contactId: string) =>
    jsonFetch<OmniResponse>(`/api/transactions/${draftId}/select`, {
      method: "POST",
      body: JSON.stringify({ contact_id: contactId }),
    }),
  confirmContact: (draftId: string) =>
    jsonFetch<OmniResponse>(`/api/contacts/${draftId}/confirm`, {
      method: "POST",
    }),
  cancelContact: (draftId: string) =>
    jsonFetch<OmniResponse>(`/api/contacts/${draftId}/cancel`, {
      method: "POST",
    }),
  confirmSchedule: (draftId: string, otp: string, sourceAccountId?: string) =>
    jsonFetch<OmniResponse>(`/api/schedules/${draftId}/confirm`, {
      method: "POST",
      body: JSON.stringify({ otp, source_account_id: sourceAccountId }),
    }),
  cancelSchedule: (draftId: string) =>
    jsonFetch<OmniResponse>(`/api/schedules/${draftId}/cancel`, {
      method: "POST",
    }),
  stt: async (audio: Blob): Promise<string> => {
    const form = new FormData();
    const ext = audio.type.includes("webm")
      ? "webm"
      : audio.type.includes("ogg")
        ? "ogg"
        : audio.type.includes("mp4")
          ? "m4a"
          : "wav";
    form.append("audio", audio, `recording.${ext}`);
    const res = await fetch("/api/speech/stt", {
      method: "POST",
      headers: { "x-user-id": "u_an" }, // no Content-Type for FormData
      body: form,
    });
    if (!res.ok) {
      let detail = "";
      try {
        detail = (await res.json()).detail ?? "";
      } catch {
        /* ignore */
      }
      throw new Error(`${res.status} ${detail || res.statusText}`);
    }
    const data = (await res.json()) as { text: string };
    return data.text;
  },
  recentRecipients: async (max = 5): Promise<RecentRecipient[]> => {
    const txs = await jsonFetch<
      { id: string; created_at: string; contact: RecentRecipient["contact"] }[]
    >("/api/transactions?limit=50");
    const seen = new Set<string>();
    const out: RecentRecipient[] = [];
    for (const t of txs) {
      if (!t.contact?.id || seen.has(t.contact.id)) continue;
      seen.add(t.contact.id);
      out.push({ contact: t.contact, last_at: t.created_at });
      if (out.length >= max) break;
    }
    return out;
  },
};
