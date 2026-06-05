import type { AtmHit } from "../types";

interface Props {
  atms: AtmHit[];
}

// Bank-logo emojis. Mock for the demo — keeps the card identifiable at
// a glance without depending on remote SVGs. Falls back to a generic
// bank icon when the issuer isn't in our seeded set.
const BANK_EMOJI: Record<string, string> = {
  Vietcombank: "🟢",
  BIDV: "🔵",
  Techcombank: "🔴",
  "MB Bank": "🟣",
  VPBank: "🟢",
  Agribank: "🟤",
  ACB: "🟡",
  Sacombank: "🔶",
};

const formatDistance = (km?: number) => {
  if (km == null) return null;
  if (km < 1) return `${Math.round(km * 1000)} m`;
  return `${km.toFixed(2)} km`;
};

/**
 * List of mock ATM / branch entries returned by ``OmniResponse.atms``.
 * Sorted ascending by ``distance_km`` when the chat path enriched the
 * response with the user's location. Tapping a row opens Google Maps in
 * a new tab — the only place we reach for an external dependency, and
 * we do it as a plain URL so no API key / SDK is needed.
 */
export const AtmCard = ({ atms }: Props) => {
  if (!atms || atms.length === 0) return null;
  return (
    <div className="atm-card" aria-label="Danh sách ATM gần đây">
      <div className="atm-card__title">
        <span aria-hidden="true">📍</span> ATM &amp; chi nhánh
      </div>
      <ul className="atm-card__list">
        {atms.map((a) => {
          const emoji = BANK_EMOJI[a.bank] ?? "🏦";
          const dist = formatDistance(a.distance_km);
          const href = `https://maps.google.com/?q=${a.lat},${a.lng}`;
          return (
            <li key={a.id} className="atm-card__row">
              <a
                href={href}
                target="_blank"
                rel="noopener noreferrer"
                className="atm-card__link"
              >
                <div className="atm-card__head">
                  <span className="atm-card__emoji" aria-hidden="true">
                    {emoji}
                  </span>
                  <div className="atm-card__name">{a.name}</div>
                  {dist && (
                    <div className="atm-card__dist" aria-label={`Cách bạn ${dist}`}>
                      {dist}
                    </div>
                  )}
                </div>
                <div className="atm-card__meta">
                  <span className="atm-card__bank">{a.bank}</span>
                  <span aria-hidden="true"> · </span>
                  <span className="atm-card__hours">{a.hours}</span>
                </div>
                <div className="atm-card__addr">{a.address}</div>
              </a>
            </li>
          );
        })}
      </ul>
    </div>
  );
};
