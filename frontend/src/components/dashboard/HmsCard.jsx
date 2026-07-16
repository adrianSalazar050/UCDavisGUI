import Card from "../ui/Card.jsx";
import Stack from "../ui/Stack.jsx";
import StatusPill from "../ui/StatusPill.jsx";

// General HMS lookup page (same one referenced in bambu_link.py).
const HMS_WIKI =
  "https://wiki.bambulab.com/en/x1/troubleshooting/how-to-enter-hms-code";

export default function HmsCard({ summary }) {
  const codes = summary?.hms ?? [];
  return (
    <Card title="HMS errors">
      {codes.length === 0 ? (
        <div className="ui-stattile__sub">No errors</div>
      ) : (
        <Stack gap={2}>
          {codes.map((code, i) => (
            <a key={`${code}-${i}`} href={HMS_WIKI} target="_blank" rel="noreferrer">
              <StatusPill status="danger">{code}</StatusPill>
            </a>
          ))}
        </Stack>
      )}
    </Card>
  );
}
