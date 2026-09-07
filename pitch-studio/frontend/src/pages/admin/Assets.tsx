import { useState } from "react";
import { api } from "../../api";
import DataTable from "./DataTable";

export default function Assets() {
  const [message, setMessage] = useState("");
  const sync = async () => {
    setMessage("Syncing…");
    try {
      const counts = (await api.syncAssets("report")) as {
        stored: number;
        external: number;
        gap: number;
        error: number;
        skipped: number;
      };
      setMessage(
        `stored ${counts.stored}, external ${counts.external}, gap ${counts.gap}, error ${counts.error}, skipped ${counts.skipped}`,
      );
    } catch (err) {
      setMessage(err instanceof Error ? err.message : "Sync failed");
    }
  };
  return (
    <DataTable
      title="Assets"
      path="/api/admin/assets"
      extra={
        <button className="rounded border border-ink/15 px-3 py-2 text-sm" type="button" onClick={sync}>
          {message || "Sync report files"}
        </button>
      }
      fields={[
        { key: "type", label: "Type", type: "select", options: ["video", "photo", "report"] },
        { key: "title", label: "Title" },
        { key: "file_status", label: "File status" },
        { key: "matrix_ref", label: "Matrix" },
        { key: "url", label: "URL" },
        { key: "source_url", label: "Source URL" },
        { key: "module_ids", label: "Module IDs" },
        { key: "audiences", label: "Audiences" },
        { key: "status", label: "Status", type: "select", options: ["exists", "partial", "gap"] },
        { key: "notes", label: "Notes", type: "textarea" },
      ]}
    />
  );
}
