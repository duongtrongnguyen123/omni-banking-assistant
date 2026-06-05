# Omni frontend — React + Vite + TypeScript

Phone-frame chat UI for the natural-language banking assistant. No global
state library; React hooks + localStorage are enough.

```bash
npm install
npm run dev      # localhost:5173 — proxies /api and /ws to :8000
```

`npm run build` for production. Output → `dist/`.

## Layout

```
src/
  App.tsx                phone shell, message list, draft action handlers
  api/client.ts          thin fetch wrapper (typed responses)
  types.ts               TS mirror of backend Pydantic schemas
  styles/app.css         single CSS file (phone frame + components)

  components/
    Message.tsx          one message bubble + structured card (TX/history/balance/etc.)
    OmniAvatar.tsx       circular brand mark
    TransactionCard.tsx  draft card: recipient, amount, source-account picker,
                         OTP / biometric step-up, animated success state + confetti
    DisambiguationCard.tsx   "which Minh?" picker
    HistoryCard.tsx      monthly aggregate / per-contact / categories
    BalanceCard.tsx      total + per-account breakdown
    ScheduleCard.tsx     recurring schedule draft + cron-label
    ContactPicker.tsx    full-frame contacts modal (ranked by suggester)
    RecurringList.tsx    detected monthly patterns + "Đặt lịch tự động" prefill
    InsightsCard.tsx     sidebar: MoM / anomalies / subscriptions
    SuggestionStrip.tsx  top-5 next-recipient chips above input
    VoiceButton.tsx      Web Speech API (vi-VN) input
    QuickScenarios.tsx   KB1–KB9 demo shortcuts
    RepeatLastCTA.tsx    one-tap "Lặp lại lần trước" pill after first transfer

  lib/
    tts.ts               browser speechSynthesis vi-VN wrapper, opt-in toggle

  types/
    speech.d.ts          ambient SpeechRecognition types (DOM lib lacks these)
```

## Conventions

- **Vietnamese first.** All copy in `vi-VN`; English is a toggle, not a default.
- **No animation libraries.** All keyframes hand-rolled in `styles/app.css` —
  judges can read package.json without surprise dependencies.
- **No global state library.** `useState` + lift-up + `localStorage` for
  `omni.tts.enabled`, `omni.lang` etc.
- **Graceful feature detection.** Voice button hides itself if
  `webkitSpeechRecognition` is missing. TTS toggle hides if
  `speechSynthesis` is missing.

## Browser compat

| Feature | Chrome | Safari | Firefox |
|---------|--------|--------|---------|
| Web Speech input (vi-VN) | ✅ | ✅ (iOS 14.5+) | ❌ (button hidden) |
| speechSynthesis (vi-VN) | ✅ "Google Tiếng Việt" | ✅ "Linh" | ⚠ may lack vi voice |
| Confetti animation | ✅ | ✅ | ✅ |

## Where to look first

| Question | File |
|----------|------|
| "How does the chat send a message?" | `App.tsx:send` |
| "How does confirm work?" | `App.tsx:sendDraftAction` → `client.confirm` |
| "Where's the OTP UI?" | `TransactionCard.tsx` |
| "How does the success animation fire?" | `TransactionCard.tsx` + `.tx-card--success` in `app.css` |
| "Where do new recipients come from?" | `SuggestionStrip.tsx` + `client.suggestions` |
