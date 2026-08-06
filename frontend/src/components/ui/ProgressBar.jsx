// A print's progress as one object instead of three tiles.
//
// Layer, percent and remaining time were three separate StatTiles telling one
// story, and the number an operator walking past actually wants -- how far
// along is it -- was the hardest of the three to read at a glance.
//
// `value` is a percentage in 0..100, or null when the printer isn't reporting
// one; null renders the track empty rather than at zero, because "not printing"
// and "0% done" are different facts.
export default function ProgressBar({ value, label, right }) {
  const known = value != null;
  return (
    <div className="progress">
      <div className="progress__head">
        <span className="progress__label">{label}</span>
        {right && <span className="progress__right">{right}</span>}
      </div>
      <div
        className="progress__track"
        role="progressbar"
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={known ? value : undefined}
        aria-valuetext={known ? `${Math.round(value)}%` : "not printing"}
      >
        {known && (
          <div className="progress__fill" style={{ width: `${value}%` }} />
        )}
      </div>
    </div>
  );
}
