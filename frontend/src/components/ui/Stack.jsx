export default function Stack({ gap = 4, children, className = "" }) {
  return (
    <div className={`ui-stack ${className}`.trim()}
         style={{ gap: `var(--sp-${gap})` }}>
      {children}
    </div>
  );
}
