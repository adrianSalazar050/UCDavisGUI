import PageFrame from "../components/ui/PageFrame.jsx";

export default function Dashboard({ summary }) {
  return (
    <PageFrame>
      <pre>{JSON.stringify(summary, null, 2)}</pre>
    </PageFrame>
  );
}
