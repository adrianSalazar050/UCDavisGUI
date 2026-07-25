// The catalogue table. Selection is owned by the page.
export default function PartList({ parts, selectedId, onSelect }) {
  if (!parts.length) return <p className="muted">No parts yet.</p>;
  return (
    <table className="table">
      <thead>
        <tr><th>Part</th><th>Rev</th><th>Name</th><th>Model</th><th>Default recipe</th></tr>
      </thead>
      <tbody>
        {parts.map((p) => (
          <tr key={p.id} onClick={() => onSelect(p.id)}
              className={p.id === selectedId ? "selected" : ""}>
            <td>{p.part_number}</td>
            <td>{p.revision}</td>
            <td>{p.name || "—"}</td>
            <td>{p.model_filename ? p.model_filename : "—"}</td>
            <td>{p.default_recipe_id ? "set" : "—"}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
