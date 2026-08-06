import Button from "../ui/Button.jsx";

const AXES = ["x", "y", "z"];

// Rotation controls. Emits the delta via onRotate(axis, degrees) -- the math
// lives in stlGeometry.addRotation (applied by the parent) so this stays
// presentational.
//
// Every control names its axis in an aria-label: on screen the axis is a
// heading at the start of the row, but read one control at a time "+90°" three
// times over says nothing about which way the model turns.
export default function OrientControls({ rotation, onRotate, onReset, disabled }) {
  return (
    <div className="orient-controls" role="group" aria-label="Rotate the model">
      {AXES.map((axis) => (
        <div key={axis} className="orient-controls__axis">
          <span className="orient-controls__label">{axis.toUpperCase()}</span>
          <Button size="sm" disabled={disabled}
                  aria-label={`Rotate ${axis.toUpperCase()} by -90 degrees`}
                  onClick={() => onRotate(axis, -90)}>
            -90°
          </Button>
          <Button size="sm" disabled={disabled}
                  aria-label={`Rotate ${axis.toUpperCase()} by +90 degrees`}
                  onClick={() => onRotate(axis, 90)}>
            +90°
          </Button>
          <input type="range" min="0" max="359" value={rotation[axis]}
                 disabled={disabled} aria-label={`${axis.toUpperCase()} fine rotation`}
                 onChange={(e) => onRotate(axis, Number(e.target.value) - rotation[axis])} />
          <span className="orient-controls__deg">{Math.round(rotation[axis])}°</span>
        </div>
      ))}
      <Button size="sm" disabled={disabled} onClick={onReset}>Reset rotation</Button>
    </div>
  );
}
