export default function Columns({ template = "1fr 1fr", gap = 5, children }) {
  return (
    <div className="ui-columns"
         style={{ gridTemplateColumns: template, gap: `var(--sp-${gap})` }}>
      {children}
    </div>
  );
}
