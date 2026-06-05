import { useT, type StringKey } from "../i18n/strings";

interface Scenario {
  labelKey: StringKey;
  textKey: StringKey;
}

const SCENARIOS: Scenario[] = [
  { labelKey: "qsKB1", textKey: "qsKB1Text" },
  { labelKey: "qsKB2", textKey: "qsKB2Text" },
  { labelKey: "qsKB3", textKey: "qsKB3Text" },
  { labelKey: "qsKB4", textKey: "qsKB4Text" },
  { labelKey: "qsKB5", textKey: "qsKB5Text" },
  { labelKey: "qsKB6", textKey: "qsKB6Text" },
  { labelKey: "qsKB7", textKey: "qsKB7Text" },
  { labelKey: "qsKB8", textKey: "qsKB8Text" },
];

export const QuickScenarios = ({ onPick }: { onPick: (text: string) => void }) => {
  const { t } = useT();
  return (
    <div className="quick-scenarios">
      <div className="quick-scenarios__title">{t("quickScenariosTitle")}</div>
      <div className="quick-scenarios__list">
        {SCENARIOS.map((s) => (
          <button
            key={s.labelKey}
            className="quick-chip"
            onClick={() => onPick(t(s.textKey))}
          >
            {t(s.labelKey)}
          </button>
        ))}
      </div>
    </div>
  );
};
