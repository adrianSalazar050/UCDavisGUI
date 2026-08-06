import { Fragment } from "react";
import Card from "../ui/Card.jsx";

export default function PrintInfoCard({ summary }) {
  const s = summary ?? {};
  const rows = [
    ["G-code file", s.gcode_file],
    ["Job", s.subtask_name],
    ["Speed", s.spd_lvl != null ? `level ${s.spd_lvl} (${s.spd_mag ?? "?"}%)` : null],
  ];
  // Both of these exist only on a print that went wrong, and they rendered as
  // ordinary grey <dd> text between the file name and the speed level. The
  // dotless pill puts a fault in the same red the rest of the app uses for one,
  // without adding a row that is empty the rest of the time.
  if (s.print_error) {
    rows.push(["Print error",
               <span className="pill pill-bad">{String(s.print_error)}</span>]);
  }
  if (s.fail_reason) {
    rows.push(["Fail reason",
               <span className="pill pill-bad">{String(s.fail_reason)}</span>]);
  }
  return (
    <Card title="Print detail">
      <dl className="kv">
        {rows.map(([k, v]) => (
          <Fragment key={k}>
            <dt>{k}</dt>
            {/* A field the printer is not reporting reads as an em dash, never
                as a blank line: "nothing here" and "not reported" look the
                same otherwise. */}
            <dd>{v ?? "—"}</dd>
          </Fragment>
        ))}
      </dl>
    </Card>
  );
}
