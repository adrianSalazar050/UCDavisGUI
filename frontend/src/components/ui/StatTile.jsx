export default function StatTile({ label, value, sub }) {
  return (
    <div className="ui-stattile">
      <div className="ui-stattile__label">{label}</div>
      <div className="ui-stattile__value">{value ?? "—"}</div>
      {sub && <div className="ui-stattile__sub">{sub}</div>}
    </div>
  );
}
