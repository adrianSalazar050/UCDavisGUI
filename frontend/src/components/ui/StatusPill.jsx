export default function StatusPill({ status = "ok", children }) {
  return (
    <span className={`ui-pill ui-pill--${status}`}>
      <span className="ui-pill__dot" />
      {children}
    </span>
  );
}
