import { useId } from "react";

export default function Field({ label, help, error, id, className = "", ...rest }) {
  // useId() gives a stable per-instance id for the life of the component,
  // unlike a module-level counter incremented during render (which mutates
  // shared state on every render — including StrictMode's double-invoke —
  // and would hand out a NEW id on every keystroke in a controlled form).
  const autoId = useId();
  const fieldId = id ?? autoId;
  return (
    <div className="ui-field">
      <label className="ui-field__label" htmlFor={fieldId}>{label}</label>
      <input
        {...rest}
        id={fieldId}
        className={`ui-field__input ${className}`.trim()}
        aria-invalid={error ? "true" : undefined}
      />
      {error
        ? <div className="ui-field__error">{error}</div>
        : help ? <div className="ui-field__help">{help}</div> : null}
    </div>
  );
}
