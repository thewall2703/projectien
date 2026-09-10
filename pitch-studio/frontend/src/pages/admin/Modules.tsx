import { useState } from "react";
import { api } from "../../api";
import { Button } from "../../components/ui";
import DataTable from "./DataTable";

export default function Modules() {
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);
  const reseed = async () => {
    setBusy(true);
    setMessage("");
    try {
      const counts = (await api.reseed()) as {
        modules: number;
        facts: number;
        recipes: number;
        objections: number;
        assets: number;
      };
      setMessage(
        `Seeded ${counts.modules} modules, ${counts.facts} facts, ${counts.recipes} recipes, ${counts.objections} objections, ${counts.assets} assets`,
      );
    } catch (err) {
      setMessage(err instanceof Error ? err.message : "Reseed failed");
    } finally {
      setBusy(false);
    }
  };
  return (
    <DataTable
      title="Modules"
      path="/api/admin/modules"
      extra={
        <Button loading={busy} onClick={reseed}>
          {message || "Reseed from workbook"}
        </Button>
      }
      fields={[
        { key: "id", label: "ID" },
        { key: "name", label: "Name" },
        { key: "job", label: "Job", type: "textarea" },
        { key: "core_content", label: "Core content", type: "textarea" },
        { key: "flex_points", label: "Flex points", type: "textarea" },
        { key: "sources", label: "Sources" },
        { key: "owner", label: "Owner" },
        { key: "sort_order", label: "Sort", type: "number" },
      ]}
    />
  );
}
