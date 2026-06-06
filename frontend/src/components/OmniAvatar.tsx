export const OmniAvatar = ({ size = 36 }: { size?: number }) => (
  <div className="omni-avatar" style={{ width: size, height: size }}>
    {/* <img> has an implicit role="img"; the accessible name lives in alt. */}
    <img
      src="/omni-avatar.png"
      alt="Trợ lý Omni"
      width={size}
      height={size}
      draggable={false}
    />
  </div>
);
