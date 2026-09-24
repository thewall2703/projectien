import DataTable from "./DataTable";

export default function Assets() {
  return (
    <DataTable
      title="Assets"
      path="/api/admin/assets"
      searchable
      searchPlaceholder="Search by title, type, or file status…"
      entityLabel="asset"
      fields={[
        { key: "type", label: "Type", type: "select", options: ["video", "photo", "report"] },
        { key: "title", label: "Title" },
        { key: "file_status", label: "File status" },
        { key: "extract_status", label: "Extract" },
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
