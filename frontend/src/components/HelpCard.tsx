/**
 * Structured /help card.
 *
 * Renders the ``help_sections`` payload returned by the orchestrator
 * (see ``_help_response`` in ``backend/app/services/orchestrator.py``).
 * Sections with an ``items`` array become a labelled list of example
 * chips; the ``shortcuts`` section becomes a keyboard table.
 *
 * Chip clicks call ``onPrefill`` so the user can edit the example
 * before submitting — same UX contract as ``<SkillsCard />``.
 */

export interface HelpItem {
  label: string;
  example: string;
}

export interface HelpShortcut {
  keys: string;
  label: string;
}

export interface HelpSection {
  id: string;
  title: string;
  items?: HelpItem[];
  shortcuts?: HelpShortcut[];
}

interface Props {
  sections: HelpSection[];
  onPrefill?: (text: string) => void;
}

export const HelpCard = ({ sections, onPrefill }: Props) => {
  return (
    <div className="help-card" role="region" aria-label="Hướng dẫn sử dụng Omni">
      <div className="help-card__title">Omni có thể giúp bạn</div>
      {sections.map((section) => (
        <div key={section.id} className="help-card__section">
          <div className="help-card__section-title">{section.title}</div>
          {section.items && (
            <div className="help-card__items">
              {section.items.map((item) => (
                <button
                  key={item.example}
                  type="button"
                  className="help-card__chip"
                  title={item.example}
                  onClick={() => onPrefill?.(item.example)}
                >
                  <span className="help-card__chip-label">{item.label}</span>
                  <span className="help-card__chip-example">{item.example}</span>
                </button>
              ))}
            </div>
          )}
          {section.shortcuts && (
            <ul className="help-card__shortcuts">
              {section.shortcuts.map((sc) => (
                <li key={sc.keys} className="help-card__shortcut">
                  <kbd>{sc.keys}</kbd>
                  <span>{sc.label}</span>
                </li>
              ))}
            </ul>
          )}
        </div>
      ))}
    </div>
  );
};
