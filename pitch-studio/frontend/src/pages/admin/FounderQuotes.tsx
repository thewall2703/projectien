import { useState } from "react";
import { api } from "../../api";
import { Button } from "../../components/ui";
import DataTable from "./DataTable";

export default function FounderQuotes() {
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);
  const ingest = async () => {
    setBusy(true);
    setMessage("");
    try {
      const counts = (await api.ingestTranscripts()) as {
        files: number;
        chunks: number;
        kept: number;
        skipped: number;
        dropped: number;
      };
      setMessage(
        `files ${counts.files}, chunks ${counts.chunks}, kept ${counts.kept}, skipped ${counts.skipped}, dropped ${counts.dropped}`,
      );
    } catch (err) {
      setMessage(err instanceof Error ? err.message : "Ingest failed");
    } finally {
      setBusy(false);
    }
  };
  return (
    <DataTable
      title="Founder quotes"
      path="/api/admin/founder-quotes"
      extra={
        <Button loading={busy} onClick={ingest}>
          {message || "Ingest transcripts"}
        </Button>
      }
      fields={[
        { key: "text", label: "Text", type: "textarea" },
        { key: "topic", label: "Topic", type: "select", options: ["institution", "vision", "students", "challenges", "founder"] },
        { key: "module_ids", label: "Module IDs" },
        { key: "source_name", label: "Source" },
        { key: "status", label: "Status", type: "select", options: ["approved", "pending", "rejected"] },
        { key: "speaker", label: "Speaker" },
        { key: "source_url", label: "Source URL" },
        { key: "verbatim", label: "Verbatim", type: "checkbox" },
      ]}
    />
  );
}
