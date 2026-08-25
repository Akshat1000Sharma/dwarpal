/**
 * Third-party marks, drawn inline.
 *
 * These identify the services Dwarpal actually talks to. They are used nominatively, to say what
 * interoperates with what, and never to suggest endorsement. Inline SVG rather than image files
 * because the CSP on a built page forbids remote assets and because a mark that inherits
 * currentColor can sit in a heading without a second asset for the dark palette.
 *
 * Paths are the CC0 glyphs published by Simple Icons.
 */

type MarkProps = {
  className?: string;
  /** Draw in the vendor's own colour rather than inheriting currentColor. */
  colored?: boolean;
  title?: string;
};

const CLAUDE_PATH =
  "M4.709 15.955l4.72-2.647.079-.23-.08-.128H9.2l-.79-.048-2.698-.073-2.339-.097-2.266-.122-.571-.121L0 11.784l.055-.352.48-.321.686.06 1.52.103 2.278.158 1.652.097 2.449.255h.389l.055-.157-.134-.098-.103-.097-2.358-1.596-2.552-1.688-1.336-.972L2.928 6.9l-.376-.477-.163-1.042.68-.748.91.062.234.063.923.708 1.973 1.526 2.576 1.895.376.315.151-.107.019-.075-.17-.283-1.409-2.545-1.502-2.587-.669-1.07-.176-.643a3.096 3.096 0 01-.109-.758L6.618.134 7.029 0l.99.134.417.364.616 1.404.997 2.218 1.546 3.014.453.892.24.827.09.253h.157V9.06l.127-1.7.24-2.084.23-2.683.08-.755.376-.91.75-.494.585.28.483.692-.067.45-.288 1.865-.565 2.936-.368 1.965h.215l.245-.245.994-1.319 1.665-2.082.737-.828.858-.914.552-.437h1.044l.767 1.14-.344 1.177-1.075 1.362-.891 1.154-1.279 1.72-.799 1.377.074.11.19-.02 2.881-.612 1.556-.282 1.858-.318.84.39.09.397-.33.815-1.977.487-2.318.464-3.45.815-.043.03.049.061 1.554.147.665.036h1.63l3.033.226.793.523.476.64-.08.487-1.22.622-1.648-.39-3.847-.916-1.32-.33h-.183v.11l1.1 1.075 2.016 1.82 2.522 2.345.128.578-.323.457-.34-.049-2.207-1.659-.851-.748-1.926-1.62h-.128v.17l.444.649 2.345 3.521.122 1.08-.17.353-.607.213-.668-.122-1.373-1.927-1.416-2.17-1.143-1.943-.14.08-.674 7.254-.316.37-.729.28-.607-.461-.322-.747.322-1.476.389-1.924.315-1.53.286-1.9.17-.632-.012-.042-.14.018-1.434 1.967-2.18 2.945-1.726 1.845-.414.164-.717-.37.067-.662.401-.589 2.389-3.04 1.44-1.882.93-1.086-.006-.158h-.055L4.132 18.56l-1.13.146-.487-.456.061-.746.231-.243 1.908-1.312-.006.006z";

const WHATSAPP_PATH =
  "M17.472 14.382c-.297-.149-1.758-.867-2.03-.967-.273-.099-.471-.148-.67.15-.197.297-.767.966-.94 1.164-.173.199-.347.223-.644.075-.297-.15-1.255-.463-2.39-1.475-.883-.788-1.48-1.761-1.653-2.059-.173-.297-.018-.458.13-.606.134-.133.298-.347.446-.52.149-.174.198-.298.298-.497.099-.198.05-.371-.025-.52-.075-.149-.669-1.612-.916-2.207-.242-.579-.487-.5-.669-.51-.173-.008-.371-.01-.57-.01-.198 0-.52.074-.792.372-.272.297-1.04 1.016-1.04 2.479 0 1.462 1.065 2.875 1.213 3.074.149.198 2.096 3.2 5.077 4.487.709.306 1.262.489 1.694.625.712.227 1.36.195 1.871.118.571-.085 1.758-.719 2.006-1.413.248-.694.248-1.289.173-1.413-.074-.124-.272-.198-.57-.347m-5.421 7.403h-.004a9.87 9.87 0 01-5.031-1.378l-.361-.214-3.741.982.998-3.648-.235-.374a9.86 9.86 0 01-1.51-5.26c.001-5.45 4.436-9.884 9.888-9.884 2.64 0 5.122 1.03 6.988 2.898a9.825 9.825 0 012.893 6.994c-.003 5.45-4.437 9.884-9.885 9.884m8.413-18.297A11.815 11.815 0 0012.05 0C5.495 0 .16 5.335.157 11.892c0 2.096.547 4.142 1.588 5.945L.057 24l6.305-1.654a11.882 11.882 0 005.683 1.448h.005c6.554 0 11.89-5.335 11.893-11.893a11.821 11.821 0 00-3.48-8.413z";

export const CLAUDE_ORANGE = "#D97757";
export const WHATSAPP_GREEN = "#25D366";

function Mark({
  path,
  color,
  className = "h-5 w-5",
  colored = false,
  title,
}: MarkProps & { path: string; color: string }) {
  return (
    <svg
      viewBox="0 0 24 24"
      className={className}
      fill={colored ? color : "currentColor"}
      role={title ? "img" : undefined}
      aria-label={title}
      aria-hidden={title ? undefined : true}
    >
      <path d={path} />
    </svg>
  );
}

export function ClaudeMark(props: MarkProps) {
  return <Mark {...props} path={CLAUDE_PATH} color={CLAUDE_ORANGE} />;
}

export function WhatsAppMark(props: MarkProps) {
  return <Mark {...props} path={WHATSAPP_PATH} color={WHATSAPP_GREEN} />;
}

/**
 * A section heading carrying a vendor mark in a tinted plate.
 *
 * The tint is derived from the vendor colour rather than the palette, so the plate reads as
 * belonging to that service without the mark itself competing with the page's own blue.
 */
export function BrandHeading({
  mark,
  tint,
  eyebrow,
  title,
  children,
}: {
  mark: React.ReactNode;
  tint: string;
  eyebrow?: string;
  title: string;
  children?: React.ReactNode;
}) {
  return (
    <div className="flex items-start gap-3.5">
      <span
        className="grid h-11 w-11 shrink-0 place-items-center rounded-[11px] border"
        style={{ backgroundColor: `${tint}14`, borderColor: `${tint}33` }}
      >
        {mark}
      </span>
      <div className="min-w-0">
        {eyebrow && (
          <div className="text-[11px] font-semibold uppercase tracking-[0.07em] text-faint">
            {eyebrow}
          </div>
        )}
        <h2 className="mt-0.5 text-[17px] font-semibold tracking-[-0.01em] text-ink">{title}</h2>
        {children && (
          <p className="mt-1.5 text-[13px] leading-relaxed text-muted">{children}</p>
        )}
      </div>
    </div>
  );
}

/**
 * A compact "works with this" reference, for pages that mention an integration in passing rather
 * than documenting it.
 */
export function BrandChip({
  mark,
  tint,
  label,
  detail,
}: {
  mark: React.ReactNode;
  tint: string;
  label: string;
  detail: string;
}) {
  return (
    <div
      className="flex items-center gap-2.5 rounded-[10px] border px-3 py-2.5"
      style={{ backgroundColor: `${tint}0F`, borderColor: `${tint}30` }}
    >
      <span className="shrink-0">{mark}</span>
      <span className="min-w-0">
        <span className="block text-[12.5px] font-medium text-ink">{label}</span>
        <span className="block text-[11.5px] leading-snug text-muted">{detail}</span>
      </span>
    </div>
  );
}
