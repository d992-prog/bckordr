interface VeltrixMarkProps {
  className?: string;
  decorative?: boolean;
  withName?: boolean;
}

export function VeltrixMark({
  className = "",
  decorative = false,
  withName = true,
}: VeltrixMarkProps) {
  return (
    <span
      className={`vx-brand ${className}`.trim()}
      aria-label={decorative ? undefined : "Veltrix VPN"}
    >
      <svg
        className="vx-mark"
        viewBox="0 0 64 64"
        aria-hidden={decorative}
        role={decorative ? undefined : "img"}
      >
        {!decorative && <title>Veltrix VPN</title>}
        <ellipse
          className="vx-mark__lens vx-mark__lens--blue"
          cx="32"
          cy="20"
          rx="12"
          ry="20"
          transform="rotate(8 32 20)"
        />
        <ellipse
          className="vx-mark__lens vx-mark__lens--mint"
          cx="23"
          cy="36"
          rx="11"
          ry="19"
          transform="rotate(-48 23 36)"
        />
        <ellipse
          className="vx-mark__lens vx-mark__lens--lilac"
          cx="43"
          cy="38"
          rx="10"
          ry="18"
          transform="rotate(48 43 38)"
        />
      </svg>
      {withName && <span className="vx-brand__name">Veltrix VPN</span>}
    </span>
  );
}
