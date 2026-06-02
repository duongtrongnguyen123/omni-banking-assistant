export const OmniAvatar = ({ size = 36 }: { size?: number }) => (
  <div
    className="omni-avatar"
    style={{ width: size, height: size }}
    aria-label="Omni"
  >
    <svg viewBox="0 0 64 64" width={size} height={size} aria-hidden>
      <circle cx="32" cy="32" r="30" fill="#221F4B" />
      <circle cx="32" cy="36" r="22" fill="#221F4B" />
      <circle cx="22" cy="28" r="9" fill="#FFF6E9" stroke="#F58220" strokeWidth="2" />
      <circle cx="42" cy="28" r="9" fill="#FFF6E9" stroke="#F58220" strokeWidth="2" />
      <circle cx="22" cy="28" r="3" fill="#1A1A4B" />
      <circle cx="42" cy="28" r="3" fill="#1A1A4B" />
      <polygon points="29,36 35,36 32,42" fill="#F58220" />
      <ellipse cx="14" cy="40" rx="3" ry="2" fill="#F58220" opacity=".5" />
      <ellipse cx="50" cy="40" rx="3" ry="2" fill="#F58220" opacity=".5" />
    </svg>
  </div>
);
