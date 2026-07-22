import { cloneElement, useId } from "react";

// `children` replaces the <input> for controls that aren't one (a <select>,
// say) while keeping the label/help/error furniture and the id wiring
// identical -- so a dropdown lines up with the text fields beside it instead
// of re-implementing the same markup with the same class names.
export default function Field({ label, help, error, id, className = "",
                               children, ...rest }) {
  // useId() gives a stable per-instance id for the life of the component,
  // unlike a module-level counter incremented during render (which mutates
  // shared state on every render — including StrictMode's double-invoke —
  // and would hand out a NEW id on every keystroke in a controlled form).
  const autoId = useId();
  const fieldId = id ?? autoId;
  return (
    <div className="ui-field">
      <label className="ui-field__label" htmlFor={fieldId}>{label}</label>
      {children
        ? cloneElement(children, {
            id: fieldId,
            className: `ui-field__input ${className}`.trim(),
            "aria-invalid": error ? "true" : undefined,
          })
        : (
          <input
            {...rest}
            id={fieldId}
            className={`ui-field__input ${className}`.trim()}
            aria-invalid={error ? "true" : undefined}
          />
        )}
      {error
        ? <div className="ui-field__error">{error}</div>
        : help ? <div className="ui-field__help">{help}</div> : null}
    </div>
  );
}
