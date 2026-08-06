import Button from "../ui/Button.jsx";
import EmptyState from "../ui/EmptyState.jsx";

// The catalogue table. Selection is owned by the page, and `onAdd` opens the
// page's add-a-part disclosure -- an empty catalogue is the one moment where
// the form is the only useful thing on the page, so the empty state offers it
// rather than leaving the reader to find it below the table that isn't there.
export default function PartList({ parts, selectedId, onSelect, onAdd }) {
  if (!parts.length) {
    return (
      <EmptyState
        title="No parts in the catalogue"
        action={onAdd && (
          <Button variant="primary" onClick={onAdd}>Add the first part</Button>
        )}
      >
        A part is a part number and a revision — BRK-100 rev A — holding one
        model file and the slice recipes that turn it into a printable job.
        The rest of the lab hangs off it: you slice from a part, and the run
        and the pieces the printer records point back to it.
      </EmptyState>
    );
  }
  return (
    <table className="table table--selectable">
      <thead>
        <tr><th>Part</th><th>Rev</th><th>Name</th><th>Model file</th><th>Default recipe</th></tr>
      </thead>
      <tbody>
        {parts.map((p) => (
          <tr key={p.id} onClick={() => onSelect(p.id)}
              className={p.id === selectedId ? "selected" : ""}>
            <td>{p.part_number}</td>
            <td>{p.revision}</td>
            <td>{p.name || <span className="muted">—</span>}</td>
            <td>{p.model_filename || <span className="muted">none yet</span>}</td>
            <td>{p.default_recipe_id ? "set" : <span className="muted">none</span>}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
