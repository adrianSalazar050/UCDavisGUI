import { Fragment } from "react";
import Card from "../ui/Card.jsx";

export default function PrintInfoCard({ summary }) {
  const s = summary ?? {};
  const rows = [
    ["G-code file", s.gcode_file],
    ["Job", s.subtask_name],
    ["Speed", s.spd_lvl != null ? `level ${s.spd_lvl} (${s.spd_mag ?? "?"}%)` : null],
  ];
  if (s.print_error) rows.push(["Print error", String(s.print_error)]);
  if (s.fail_reason) rows.push(["Fail reason", String(s.fail_reason)]);
  return (
    <Card title="Print">
      <dl className="kv">
        {rows.map(([k, v]) => (
          <Fragment key={k}>
            <dt>{k}</dt>
            <dd>{v ?? "—"}</dd>
          </Fragment>
        ))}
      </dl>
    </Card>
  );
}
