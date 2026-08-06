import Card from "../ui/Card.jsx";
import Stack from "../ui/Stack.jsx";
import StatusPill from "../ui/StatusPill.jsx";

// General HMS lookup page (same one referenced in bambu_link.py).
const HMS_WIKI =
  "https://wiki.bambulab.com/en/x1/troubleshooting/how-to-enter-hms-code";

export default function HmsCard({ summary }) {
  const codes = summary?.hms ?? [];
  return (
    <Card title="Machine health">
      {codes.length === 0 ? (
        // A healthy machine has to LOOK healthy. This was a 12px grey
        // sub-label reading "No errors", which on a card of its own was
        // indistinguishable from a card that had failed to load. The green
        // pill says it in the vocabulary every other status in the app uses.
        <StatusPill status="ok">No HMS errors</StatusPill>
      ) : (
        <Stack gap={2}>
          {codes.map((code, i) => (
            <a key={`${code}-${i}`} href={HMS_WIKI} target="_blank" rel="noreferrer">
              <StatusPill status="danger">{code}</StatusPill>
            </a>
          ))}
          {/* A pill inside a link does not look like a link, and the code is
              useless without the table it opens. */}
          <p className="muted">Each code opens Bambu’s HMS lookup.</p>
        </Stack>
      )}
    </Card>
  );
}
