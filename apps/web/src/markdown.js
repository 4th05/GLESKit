const DIRECTION_TOKEN_RE = /\[\[(up|down):([A-Za-z0-9_.+\-/]+)\]\]/gi;
// This is intentionally narrow: it recognizes only the portable form emitted
// by GLESKit reports. The input has already been HTML-escaped by the Markdown
// renderer, so accepting arbitrary tags or styles here would be unsafe.
const PORTABLE_DIRECTION_SPAN_RE = /&lt;span\s+style=&quot;color:\s*#(b42318|175cd3)\s*;?&quot;&gt;([↑↓])\s+([A-Za-z0-9_.+\-/]+)&lt;\/span&gt;/gi;
const PROTECTED_MARKUP_RE = /(<code\b[^>]*>[\s\S]*?<\/code>|<img\b[^>]*>|<a\b[^>]*>|<\/a>)/gi;

function directionalGeneMarkup(direction, rawGene) {
  const label = direction === "up" ? "UP" : "DOWN";
  const arrow = direction === "up" ? "↑" : "↓";
  return `<span class="gene-direction gene-direction-${direction}" data-gene-direction="${direction}" aria-label="${label} gene">${label} <span class="gene-direction-arrow" aria-hidden="true">${arrow}</span> ${rawGene}</span>`;
}

/**
 * Render the small, report-safe directional-gene Markdown extension.
 *
 * Input is expected to have passed through the Markdown renderer's HTML
 * escaping step. It supports the legacy token form and the portable inline
 * HTML form (`<span style="color:#B42318">↑ GENE</span>`). Only those two
 * fixed colours, arrows, and gene-safe labels are recognized; arbitrary HTML
 * remains escaped. Existing code spans and HTML tag attributes are protected
 * so a literal example or a link target is never restyled.
 */
export function renderDirectionalGeneMarkdown(value) {
  return String(value || "")
    .split(PROTECTED_MARKUP_RE)
    .map((segment) => {
      if (!segment || /^<(?:code\b|img\b|a\b|\/a>)/i.test(segment)) {
        return segment;
      }
      return segment
        .replace(PORTABLE_DIRECTION_SPAN_RE, (_match, rawColor, rawArrow, rawGene) => {
          const direction = String(rawColor || "").toLowerCase() === "b42318" ? "up" : "down";
          const expectedArrow = direction === "up" ? "↑" : "↓";
          return rawArrow === expectedArrow ? directionalGeneMarkup(direction, rawGene) : _match;
        })
        .replace(DIRECTION_TOKEN_RE, (_match, rawDirection, rawGene) => {
          return directionalGeneMarkup(String(rawDirection || "").toLowerCase(), rawGene);
        });
    })
    .join("");
}
